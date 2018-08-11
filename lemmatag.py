#!/usr/bin/env python3
import numpy as np
import tensorflow as tf
import morpho_dataset
from utils import MorphoAnalyzer, Tee, log_time
from pprint import pprint
import argparse
import datetime
import os
import re
import shutil
import sys
from tqdm import tqdm
from tensorflow.python.client import timeline
import logging
from logging import warning, info, debug, error


def find_first(vec, val, transpose=True):
    if transpose:
        vec = tf.transpose(vec)
    #vec = tf.Print(vec, [tf.shape(vec)])
    vheight = tf.shape(vec)[1]
    vlen = tf.shape(vec)[0]

    def step(st, col):
        ix, poss = st
        #col = tf.Print(col, [ix, tf.shape(col), col])
        eqs = tf.cast(tf.equal([val], col), dtype=tf.int32)
        return (ix + 1, tf.minimum(poss, ix * eqs + vlen * (1 - eqs)))

    return tf.scan(step, vec, (0, vlen + tf.zeros(vheight, dtype=tf.int32)))[1][-1, :]


class FixedBeamSearchDecoder(tf.contrib.seq2seq.BeamSearchDecoder):
    def finalize(self, outputs, final_state, sequence_lengths):
        # BeamSearchDecoder does not follow the correct semantics of the the finished flag
        # which results in taking wrong length here and getting wrong decoded string.
        # We substitute the sequence length recorded by dynamic_decoder (which is wrong because
        # of the wrong finished flag returned by BeamSearchDecoder.step) with the length
        # recorded in BeamSearchState which is correct.
        # See https://github.com/tensorflow/tensorflow/issues/13536
        return super().finalize(outputs, final_state, final_state.lengths)


class AddInputsWrapper(tf.nn.rnn_cell.RNNCell):
    def __init__(self, cell, extra_input, name=None, **kwargs):
        super(AddInputsWrapper, self).__init__(name=name)
        self._cell = cell = cell
        self._extra_input = extra_input

    def __call__(self, inputs, state, scope=None):
        inputs_exp = tf.concat([inputs, self._extra_input], axis=-1)
        return self._cell.__call__(inputs_exp, state, scope=scope)

    @property
    def state_size(self):
        return self._cell.state_size

    @property
    def output_size(self):
        return self._cell.output_size

    def zero_state(self, batch_size, dtype):
        return self._cell.zero_state(batch_size, dtype)


class Network:
    def __init__(self, threads, seed=42):
        # Create an empty graph and a session
        graph = tf.Graph()
        graph.seed = seed
        self.session = tf.Session(graph=graph, config=tf.ConfigProto(inter_op_parallelism_threads=threads,
                                                                     intra_op_parallelism_threads=threads))

    def construct(self, args, num_words, num_chars, lem_num_chars, num_tags, num_senses, bow, eow):
        with self.session.graph.as_default():
            # Training params
            self.is_training = tf.placeholder(tf.bool, [])
            self.learning_rate = tf.placeholder(tf.float32, [], name="learning_rate")

            # Sentence lengths
            self.sentence_lens = tf.placeholder(tf.int32, [None], name="sentence_lens")
            # Number of output words
            self.words_count = tf.reduce_sum(self.sentence_lens)
            words_count = self.words_count
            # Map sentences -> word list
            self.word_indexes = tf.placeholder(tf.int32, [None, 2], name='word_indexes')

            # Tag data
            self.tags = tf.placeholder(tf.int32, [None, None, len(num_tags)], name="tags")

            # Form IDs and charseqs
            self.word_ids = tf.placeholder(tf.int32, [None, None], name="word_ids")
            self.charseqs = tf.placeholder(tf.int32, [None, None], name="charseqs")
            self.charseq_lens = tf.placeholder(tf.int32, [None], name="charseq_lens")
            self.charseq_ids = tf.placeholder(tf.int32, [None, None], name="charseq_ids")

            # Lemma charseqs
            self.target_senses = tf.placeholder(tf.int32, [None, None], name="target_senses")
            self.target_ids = tf.placeholder(tf.int32, [None, None], name="target_ids")
            self.target_seqs = tf.placeholder(tf.int32, [None, None], name="target_seqs")
            self.target_seq_lens = tf.placeholder(tf.int32, [None], name="target_seq_lens")

            # Sentence weights
            weights = tf.sequence_mask(self.sentence_lens, dtype=tf.float32)
            sum_weights = tf.reduce_sum(weights)

            # Source forms lengths (in sentences and by words/lemmas)
            sentence_form_len = tf.nn.embedding_lookup(self.charseq_lens, self.charseq_ids)
            word_form_len = tf.gather_nd(sentence_form_len, self.word_indexes)

            # Target sequences for words
            _target_seq_lens = tf.nn.embedding_lookup(self.target_seq_lens, self.target_ids) # 2D
            _target_seqs = tf.nn.embedding_lookup(self.target_seqs, self.target_ids)
            # Flattened to word-list
            target_lens = tf.gather_nd(_target_seq_lens, self.word_indexes)
            target_seqs = tf.gather_nd(_target_seqs, self.word_indexes)
            target_senses = tf.gather_nd(self.target_senses, self.word_indexes)
            # Add eow at the end
            target_seqs = tf.reverse_sequence(target_seqs, target_lens, 1)
            target_seqs = tf.pad(target_seqs, [[0, 0], [1, 0]], constant_values=eow)
            target_lens = target_lens + 1
            target_seqs = tf.reverse_sequence(target_seqs, target_lens, 1)

            # RNN Cell
            if args.rnn_cell == "LSTM":
                rnn_cell = tf.nn.rnn_cell.BasicLSTMCell
#                def rnn_cell(*a, **kw):
#                    return tf.nn.rnn_cell.BasicLSTMCell(*a, state_is_tuple=False, **kw)
            elif args.rnn_cell == "GRU":
                rnn_cell = tf.nn.rnn_cell.GRUCell
            else:
                raise ValueError("Unknown rnn_cell {}".format(args.rnn_cell))

            # Create one word embedding
            def embed_words(name="common"):
                matrix_word_embeddings = tf.get_variable("word_embeddings_{}".format(name), shape=[num_words, args.we_dim], dtype=tf.float32)
                # [sentences, words, dim]
                return tf.nn.embedding_lookup(matrix_word_embeddings, self.word_ids)

            # Character-level embeddings
            def embed_characters(name="common"):
                with tf.variable_scope("char_embed_{}".format(name)):
                    character_embeddings = tf.get_variable("character_embeddings_{}".format(name), shape=[num_chars, args.cle_dim], dtype=tf.float32)
                    characters_embedded = tf.nn.embedding_lookup(character_embeddings, self.charseqs)
                    characters_embedded = tf.layers.dropout(characters_embedded, rate=args.dropout, training=self.is_training)
                    (output_fwd, output_bwd), (state_fwd, state_bwd) = tf.nn.bidirectional_dynamic_rnn(
                        tf.nn.rnn_cell.GRUCell(args.cle_dim), tf.nn.rnn_cell.GRUCell(args.cle_dim),
                        characters_embedded, sequence_length=self.charseq_lens, dtype=tf.float32)
                    cle_states = tf.concat([state_fwd, state_bwd], axis=1)
                    cle_outputs = tf.concat([output_fwd, output_bwd], axis=1)
                    sentence_cle_states = tf.nn.embedding_lookup(cle_states, self.charseq_ids)
                    sentence_cle_outputs = tf.nn.embedding_lookup(cle_outputs, self.charseq_ids)
                    word_cle_states = tf.gather_nd(sentence_cle_states, self.word_indexes)
                    word_cle_outputs = tf.gather_nd(sentence_cle_outputs, self.word_indexes)
                    return sentence_cle_states, word_cle_outputs, word_cle_states

            if args.separate_embed:
                # [sentences, words, dim], [words, char, dim], [words, dim]
                rnn_inputs_lemmas, word_cle_outputs, word_cle_states = embed_characters(name="lemmas")
                rnn_inputs_lemmas += embed_words(name="lemmas")
                rnn_inputs_tags = embed_words(name="tags") + embed_characters(name="tags")[0]
            else:
                # [sentences, words, dim], [words, char, dim], [words, dim]
                rnn_inputs_lemmas, word_cle_outputs, word_cle_states = embed_characters(name="common")
                rnn_inputs_lemmas += embed_words(name="common")
                rnn_inputs_tags = rnn_inputs_lemmas

            # Sentence-level RNN computation
            def compute_rnn(name, inputs):
                hidden_layer = tf.layers.dropout(inputs, rate=args.dropout, training=self.is_training)
                for i in range(args.rnn_layers):
                    with tf.variable_scope("word-level-rnn-{}".format(name)):
                        (hidden_layer_fwd, hidden_layer_bwd), _ = tf.nn.bidirectional_dynamic_rnn(
                            rnn_cell(args.rnn_cell_dim), rnn_cell(args.rnn_cell_dim),
                            hidden_layer, sequence_length=self.sentence_lens, dtype=tf.float32,
                            scope="word-level-rnn-{}-{}".format(name, i))
                        hidden_layer += tf.layers.dropout(hidden_layer_fwd + hidden_layer_bwd, rate=args.dropout, training=self.is_training)
                return hidden_layer

            if args.separate_rnn:
                sentence_rnn_outputs_tags = compute_rnn("tags", rnn_inputs_tags)
                _sentence_rnn_outputs_lemma = compute_rnn("lemmas", rnn_inputs_lemmas)
                word_rnn_outputs = tf.gather_nd(_sentence_rnn_outputs_lemma, self.word_indexes)
            else:
                sentence_rnn_outputs_tags = compute_rnn("common", rnn_inputs_tags)
                word_rnn_outputs = tf.gather_nd(sentence_rnn_outputs_tags, self.word_indexes)

            # Tag predictions, loss and accuracy
            def compute_tags():
                self.predictions, loss_tag = [], 0
                self.tag_outputs = []
                for i, (tags, weight) in enumerate(num_tags):
                    output_layer = tf.layers.dense(sentence_rnn_outputs_tags, tags)
                    self.tag_outputs.append(output_layer)
                    self.predictions.append(tf.argmax(output_layer, axis=2, output_type=tf.int32))

                    # Training
                    if args.label_smoothing:
                        gold_labels = tf.one_hot(self.tags[:, :, i], tags) * (1 - args.label_smoothing) + args.label_smoothing / tags
                        loss_tag += tf.losses.softmax_cross_entropy(gold_labels, output_layer, weights=weights) * weight
                    else:
                        loss_tag += tf.losses.sparse_softmax_cross_entropy(self.tags[:, :, i], output_layer, weights=weights) * weight
                self.tag_outputs = tf.concat(self.tag_outputs, axis=-1)  # Tagger output features for lemmatizer
                self.predictions = tf.stack(self.predictions, axis=-1)

                correct_tag = tf.reduce_sum(tf.cast(tf.reduce_all(
                    tf.logical_or(tf.equal(self.tags, self.predictions), tf.logical_not(args.tags.accuracy_mask())),
                    axis=2), tf.float32) * weights) / sum_weights
                correct_tags_compositional = tf.reduce_sum(tf.cast(tf.reduce_all(tf.equal(  # Average accuracy of all tags
                    self.tags, self.predictions), axis=2), tf.float32) * weights) / sum_weights

                self.current_accuracy_tag, self.update_accuracy_tag = tf.metrics.mean(correct_tag, weights=sum_weights)
                self.current_accuracy_tags_compositional, self.update_accuracy_tags_compositional = tf.metrics.mean(correct_tags_compositional)
                return loss_tag

            loss_tag = compute_tags()

            # Tag encoder for lemmatizer
            tag_features = self.tag_outputs
            tag_features = tf.stop_gradient(tag_features)
            #tag_features = tf.layers.dropout(tag_features, rate=args.dropout, training=self.is_training)
            tag_features = tf.layers.dense(tag_features, args.rnn_cell_dim, activation=tf.nn.relu)
            tag_features = tf.layers.dropout(tag_features, rate=args.dropout, training=self.is_training)
            tag_features = tf.gather_nd(tag_features, self.word_indexes)
            # Renormed word-level signal dropout
            if args.tag_signal_dropout:
                tag_features = tf.layers.dropout(tag_features, noise_shape=[words_count, 1], rate=args.tag_signal_dropout, training=self.is_training) * (1 - tf.cast(self.is_training, tf.float32) * args.tag_signal_dropout)
            # Switch the data rather the shapes
            if not args.tags_to_lemmas:
                tag_features = tf.zeros([words_count, args.rnn_cell_dim], dtype=tf.float32)

            # Lemma decoder
            def decode_lemmas():
                # Target embedding and target sequences
                tchar_emb = tf.get_variable('tchar_emb', [lem_num_chars, args.cle_dim])
                target_seqs_bow = tf.pad(target_seqs, [[0, 0], [1, 0]], constant_values=bow)[:, :-1]
                tseq_emb = tf.nn.embedding_lookup(tchar_emb, target_seqs_bow)

                decoder_layer = tf.layers.Dense(lem_num_chars, name="decoder_layer")
                base_cell = rnn_cell(args.rnn_cell_dim, name="decoder_cell")

                def create_attn_cell(beams=None):
                    with tf.variable_scope("lem_cell", reuse=tf.AUTO_REUSE):
                        def btile(x):
                            return tf.contrib.seq2seq.tile_batch(x, beams) if beams else x
                        cell = base_cell
                        cell = AddInputsWrapper(cell, btile(word_rnn_outputs)) # Already dropouted
                        cell = AddInputsWrapper(cell, btile(tag_features)) # Already dropouted
                        #do_inp_size = word_cle_states.shape[-1] + tchar_emb.shape[-1]
                        #cell = tf.contrib.rnn.DropoutWrapper(cell, input_size=do_inp_size, output_keep_prob=1.0 - args.dropout, input_keep_prob=1.0 - args.dropout, variational_recurrent=True, dtype=tf.float32)
                        cell = AddInputsWrapper(cell, btile(word_cle_states))
                        att = tf.contrib.seq2seq.LuongAttention(args.rnn_cell_dim, btile(word_cle_outputs), memory_sequence_length=btile(word_form_len))
                        cell = tf.contrib.seq2seq.AttentionWrapper(cell, att, output_attention=False)
                        return cell

                train_cell = create_attn_cell()
                pred_cell = create_attn_cell(args.beams) # Reuses the attenrion memory

                if args.rnn_cell == "LSTM":
                    #initial_lstm_gate = tf.get_variable('initial_lstm_c', [1, args.rnn_cell_dim])
                    initial_state = tf.nn.rnn_cell.LSTMStateTuple(c=word_cle_states, h=word_cle_states)
                else:
                    initial_state = word_cle_states

                # Training
                with tf.variable_scope("lem_decoder", reuse=tf.AUTO_REUSE):
                    train_helper = tf.contrib.seq2seq.TrainingHelper(tseq_emb, sequence_length=target_lens, name="train_helper")
                    train_initial_state = train_cell.zero_state(words_count, tf.float32).clone(cell_state=initial_state)
                    train_decoder = tf.contrib.seq2seq.BasicDecoder(cell=train_cell, helper=train_helper, output_layer=decoder_layer, initial_state=train_initial_state)
                    train_outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder=train_decoder)
                    train_logits = train_outputs.rnn_output
                    self.lemma_predictions_training = train_outputs.sample_id

                # Compute loss with smoothing
                with tf.variable_scope("lem_loss"):
                    weights_reshaped = tf.reshape(tf.sequence_mask(target_lens, dtype=tf.float32), [-1])
                    if args.lem_smoothing:
                        train_logits_reshaped = tf.reshape(train_logits, [-1, train_logits.shape[-1]])
                        gold_lemma_onehot = tf.one_hot(tf.reshape(target_seqs, [-1]), lem_num_chars)
                        loss_lem = tf.losses.softmax_cross_entropy(gold_lemma_onehot, train_logits_reshaped, weights=weights_reshaped, label_smoothing=args.lem_smoothing)
                    else:
                        loss_lem = tf.losses.sparse_softmax_cross_entropy(target_seqs, train_logits, weights=tf.sequence_mask(target_lens, dtype=tf.float32))

                # Predictions
                with tf.variable_scope("lem_decoder", reuse=tf.AUTO_REUSE):
                    if not args.beams:
                        pred_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(embedding=tchar_emb, start_tokens=tf.tile([bow], [words_count]), end_token=eow)
                        pred_initial_state = pred_cell.zero_state(words_count, tf.float32).clone(cell_state=initial_state)
                        pred_decoder = tf.contrib.seq2seq.BasicDecoder(cell=pred_cell, helper=pred_helper, output_layer=decoder_layer, initial_state=pred_initial_state)
                        pred_outputs, _, self.lemma_prediction_lengths = tf.contrib.seq2seq.dynamic_decode(decoder=pred_decoder, maximum_iterations=tf.reduce_max(self.charseq_lens) + 10)
                        self.lemma_predictions = tf.argmax(pred_outputs.rnn_output, axis=2, output_type=tf.int32)
                    else:
                        # Beam search predictions
                        pred_initial_state = pred_cell.zero_state(words_count * args.beams, tf.float32).clone(cell_state=tf.contrib.seq2seq.tile_batch(initial_state, args.beams))
                        pred_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
                            pred_cell, embedding=tchar_emb, start_tokens=tf.tile([bow], [words_count]),
                            end_token=eow, output_layer=decoder_layer, beam_width=args.beams,
                            initial_state=pred_initial_state, length_penalty_weight=args.beam_len_penalty)
                        dec_outputs, dec_state, dec_lens = tf.contrib.seq2seq.dynamic_decode(decoder=pred_decoder, maximum_iterations=tf.reduce_max(self.charseq_lens) + 10)
                        self.lemma_predictions = dec_outputs.predicted_ids[:, :, 0]
                        self.lemma_prediction_lengths = 1 + find_first(self.lemma_predictions, eow)

                return loss_lem

            loss_lem = decode_lemmas()

            # Sense predictions
            def compute_sense():
                if args.builtin_sense:
                    self.sense_prediction = tf.zeros([words_count], dtype=tf.int64)
                    return 0.0
                else:
                    sense_features = word_rnn_outputs
                    sense_features = tf.concat([sense_features, tag_features], axis=-1)
                    sense_layer = tf.layers.dense(sense_features, num_senses)
                    self.sense_prediction = tf.argmax(sense_layer, axis=1)
                    _gold_senses = tf.one_hot(target_senses, num_senses)
                    return tf.losses.softmax_cross_entropy(_gold_senses, sense_layer, label_smoothing=args.sense_smoothing)

            loss_sense = compute_sense()

            # Lemma predictions, loss and accuracy
            def compute_lemma_stats():

                # Training accuracy
                accuracy_training_lem = tf.reduce_all(tf.logical_or(
                    tf.equal(self.lemma_predictions_training, target_seqs),
                    tf.logical_not(tf.sequence_mask(target_lens))), axis=1)
                self.current_accuracy_lem_train, self.update_accuracy_lem_train = tf.metrics.mean(accuracy_training_lem) # , weights=sum_weights)
                accuracy_training_lemsense = tf.logical_and(
                    accuracy_training_lem,
                    tf.equal(self.sense_prediction, tf.cast(target_senses, dtype=tf.int64)))
                self.current_accuracy_lemsense_train, self.update_accuracy_lemsense_train = tf.metrics.mean(accuracy_training_lemsense) # , weights=sum_weights)

                # Predict accuracy
                minimum_length = tf.minimum(tf.shape(self.lemma_predictions)[1], tf.shape(target_seqs)[1])
                correct_lem = tf.logical_and(
                    tf.equal(self.lemma_prediction_lengths, target_lens),
                    tf.reduce_all(tf.logical_or(
                        tf.equal(self.lemma_predictions[:, :minimum_length], target_seqs[:, :minimum_length]),
                        tf.logical_not(tf.sequence_mask(target_lens, maxlen=minimum_length))), axis=1))
                self.current_accuracy_lem, self.update_accuracy_lem = tf.metrics.mean(correct_lem) # , weights=sum_weights)
                correct_lemsense = tf.logical_and(
                    correct_lem,
                    tf.equal(self.sense_prediction, tf.cast(target_senses, dtype=tf.int64)))
                self.current_accuracy_lemsense, self.update_accuracy_lemsense = tf.metrics.mean(correct_lemsense) # , weights=sum_weights)

            compute_lemma_stats()

            # Loss, training and gradients
            loss = loss_tag + loss_lem * args.loss_lem_w + loss_sense * args.loss_sense_w
            self.global_step = tf.train.create_global_step()
            self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(self.update_ops):
                optimizer = tf.contrib.opt.LazyAdamOptimizer(learning_rate=self.learning_rate, beta2=args.beta_2)
                gradients, variables = zip(*optimizer.compute_gradients(loss))
                #pprint(variables)
                self.gradient_norm = tf.global_norm(gradients)
                if args.grad_clip:
                    gradients, _ = tf.clip_by_global_norm(gradients, args.grad_clip)
                self.training = optimizer.apply_gradients(zip(gradients, variables), global_step=self.global_step, name="training")

            # Saver
            self.saver = tf.train.Saver(max_to_keep=2)

            # Summaries
            self.current_loss_tag, self.update_loss_tag = tf.metrics.mean(loss_tag, weights=sum_weights)
            self.current_loss_lem, self.update_loss_lem = tf.metrics.mean(loss_lem, weights=sum_weights)
            self.current_loss_sense, self.update_loss_sense = tf.metrics.mean(loss_sense, weights=sum_weights)
            self.current_loss, self.update_loss = tf.metrics.mean(loss, weights=sum_weights)
            self.reset_metrics = tf.variables_initializer(tf.get_collection(tf.GraphKeys.METRIC_VARIABLES))

            summary_writer = tf.contrib.summary.create_file_writer(args.logdir, flush_millis=1 * 1000)
            self.summaries = {}
            with summary_writer.as_default(), tf.contrib.summary.record_summaries_every_n_global_steps(1):
                self.summaries["train"] = [tf.contrib.summary.scalar("train/loss_tag", self.update_loss_tag),
                                           tf.contrib.summary.scalar("train/loss_sense", self.update_loss_sense),
                                           tf.contrib.summary.scalar("train/loss_lem", self.update_loss_lem),
                                           tf.contrib.summary.scalar("train/loss", self.update_loss),
                                           tf.contrib.summary.scalar("train/gradient", self.gradient_norm),
                                           tf.contrib.summary.scalar("train/accuracy_tag", self.update_accuracy_tag),
                                           tf.contrib.summary.scalar("train/accuracy_compositional_tags", self.update_accuracy_tags_compositional),
                                           tf.contrib.summary.scalar("train/accuracy_lem", self.update_accuracy_lem_train),
                                           tf.contrib.summary.scalar("train/accuracy_lemsense", self.update_accuracy_lemsense_train),
                                           tf.contrib.summary.scalar("train/learning_rate", self.learning_rate)]
            with summary_writer.as_default(), tf.contrib.summary.always_record_summaries():
                for dataset in ["dev", "test"]:
                    self.summaries[dataset] = [tf.contrib.summary.scalar(dataset + "/loss", self.current_loss),
                                               tf.contrib.summary.scalar(dataset + "/accuracy_tag", self.current_accuracy_tag),
                                               tf.contrib.summary.scalar(dataset + "/accuracy_compositional_tags", self.current_accuracy_tags_compositional),
                                               tf.contrib.summary.scalar(dataset + "/accuracy_lem", self.current_accuracy_lem),
                                               tf.contrib.summary.scalar(dataset + "/accuracy_lemsense", self.current_accuracy_lemsense)]

            # Initialize variables
            self.session.run(tf.global_variables_initializer())
            with summary_writer.as_default():
                tf.contrib.summary.initialize(session=self.session, graph=self.session.graph)

    def train_epoch(self, train, args, rate):
        first = True
        with tqdm(total=len(train.sentence_lens), file=args.realstderr) as progress_bar:
            while not train.epoch_finished():
                sentence_lens, word_ids, charseq_ids, charseqs, charseq_lens, word_indexes = train.next_batch(args.batch_size, including_charseqs=True)
                if args.word_dropout:
                    mask = np.random.binomial(n=1, p=args.word_dropout, size=word_ids[train.FORMS].shape)
                    word_ids[train.FORMS] = (1 - mask) * word_ids[train.FORMS] + mask * train.factors[train.FORMS].words_map["<unk>"]

                self.session.run(self.reset_metrics)
                # Chrome tracing graph
                if args.record_trace and first:
                    options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()
                else:
                    options = None
                    run_metadata = None
                self.session.run([self.training, self.summaries["train"]],
                                 {self.sentence_lens: sentence_lens, self.learning_rate: rate,
                                  self.charseqs: charseqs[train.FORMS], self.charseq_lens: charseq_lens[train.FORMS],
                                  self.word_ids: word_ids[train.FORMS], self.charseq_ids: charseq_ids[train.FORMS],
                                  self.target_ids: charseq_ids[train.LEMMAS], self.target_seqs: charseqs[train.LEMMAS],
                                  self.target_seq_lens: charseq_lens[train.LEMMAS], self.target_senses: word_ids[train.SENSES],
                                  self.tags: args.tags.encode(word_ids[train.TAGS], charseq_ids[train.TAGS], charseqs[train.TAGS]),
                                  self.is_training: True, self.word_indexes: word_indexes},
                                 options=options, run_metadata=run_metadata)
                progress_bar.update(len(sentence_lens))
                if args.record_trace and first:
                    fetched_timeline = timeline.Timeline(run_metadata.step_stats)
                    chrome_trace = fetched_timeline.generate_chrome_trace_format()
                    gs = self.session.run(self.global_step)
                    with open(args.logdir + '/timeline_train_{}.json'.format(gs), 'w') as f:
                        f.write(chrome_trace)
                first = False

    def evaluate(self, dataset_name, dataset, args):
        self.session.run(self.reset_metrics)
        with tqdm(total=len(dataset.sentence_lens), file=args.realstderr) as progress_bar:
            while not dataset.epoch_finished():
                sentence_lens, word_ids, charseq_ids, charseqs, charseq_lens, word_indexes = dataset.next_batch(args.batch_size, including_charseqs=True)
                self.session.run([self.update_accuracy_tag, self.update_accuracy_tags_compositional, self.update_accuracy_lem, self.update_accuracy_lemsense, self.update_loss],
                                 {self.sentence_lens: sentence_lens,
                                  self.charseqs: charseqs[dataset.FORMS], self.charseq_lens: charseq_lens[dataset.FORMS],
                                  self.word_ids: word_ids[dataset.FORMS], self.charseq_ids: charseq_ids[dataset.FORMS],
                                  self.target_ids: charseq_ids[dataset.LEMMAS], self.target_seqs: charseqs[dataset.LEMMAS],
                                  self.target_seq_lens: charseq_lens[dataset.LEMMAS], self.target_senses: word_ids[dataset.SENSES],
                                  self.tags: args.tags.encode(word_ids[dataset.TAGS], charseq_ids[dataset.TAGS], charseqs[dataset.TAGS]),
                                  self.is_training: False, self.word_indexes: word_indexes})
                progress_bar.update(len(sentence_lens))
        return self.session.run([self.current_accuracy_tag, self.current_accuracy_lem, self.current_accuracy_lemsense] + self.summaries[dataset_name])[:3]

    def predict(self, dataset, args):
        tags = []
        lemmas = []
        alphabet = dataset.factors[dataset.LEMMAS].alphabet
        sense_words = dataset.factors[dataset.SENSES].words
        with tqdm(total=len(dataset.sentence_lens), file=args.realstderr) as progress_bar:
            while not dataset.epoch_finished():
                sentence_lens, word_ids, charseq_ids, charseqs, charseq_lens, word_indexes = dataset.next_batch(args.batch_size, including_charseqs=True)
                tp, lp, lpl, senses = self.session.run(
                    [self.predictions, self.lemma_predictions, self.lemma_prediction_lengths, self.sense_prediction],
                    {self.sentence_lens: sentence_lens,
                     self.charseqs: charseqs[dataset.FORMS], self.charseq_lens: charseq_lens[dataset.FORMS],
                     self.word_ids: word_ids[dataset.FORMS], self.charseq_ids: charseq_ids[dataset.FORMS],
                     self.is_training: False, self.word_indexes: word_indexes})
                tags.extend(args.tags.decode(tp))
                for si, length in enumerate(sentence_lens):
                    lemmas.append([])
                    for i in range(length):
                        lemmas[-1].append(''.join(alphabet[lp[i][j]] for j in range(lpl[i] - 1)))
                        if not args.builtin_sense:
                            if senses[i] > 0 and sense_words[senses[i]]:
                                sword = sense_words[senses[i]]
                                if sword and sword != "<pad>":
                                    lemmas[-1][-1] += "-{}".format(sword)
                    lp, lpl, senses = lp[length:], lpl[length:], senses[length:]
                assert len(lpl) == 0
                progress_bar.update(len(sentence_lens))

        return lemmas, tags


class WholeTags:
    def __init__(self, train):
        self._train = train

    def num_tags(self):
        return [(len(self._train.factors[self._train.TAGS].words), 1.0)]

    def accuracy_mask(self):
        return [True]

    def encode(self, tag_ids, seq_ids, seqs):
        return np.expand_dims(tag_ids, -1)

    def decode(self, tags):
        result = []
        for sentence in tags:
            result.append([self._train.factors[self._train.TAGS].words[tag[0]] for tag in sentence])

        return result


class CharTags:
    def __init__(self, train, regularization_weight_compositional=1., regularization_weight_whole=1.):
        self._train = train

        self._regularization_weight_compositional = regularization_weight_compositional
        self._regularization_weight_whole = regularization_weight_whole

        self._train_alphabet = train.factors[train.TAGS].alphabet

        self._taglen = len(train.factors[train.TAGS].strings[0][0])

        self._alphabet_maps = [{"<unk>": 0} for _ in range(self._taglen)]
        self._alphabets = [["<unk>"] for _ in range(self._taglen)]
        self._cache = {}
        for tag_id, tag in enumerate(train.factors[train.TAGS].words):
            if len(tag) != self._taglen: continue

            entry = np.zeros(self._taglen + 1, dtype=np.int32)
            entry[0] = tag_id
            for i in range(self._taglen):
                if tag[i] not in self._alphabet_maps[i]:
                    self._alphabet_maps[i][tag[i]] = len(self._alphabets[i])
                    self._alphabets[i].append(tag[i])
                entry[i + 1] = self._alphabet_maps[i][tag[i]]
            self._cache[tag_id] = entry

    def num_tags(self):
        return [(len(self._train.factors[self._train.TAGS].words), self._regularization_weight_whole)] + \
               [(len(alphabet), self._regularization_weight_compositional) for alphabet in self._alphabets]

    def accuracy_mask(self):
        return [True] + [False] * self._taglen

    def encode(self, tag_ids, seq_ids, seqs):
        tags = np.zeros(seq_ids.shape + (self._taglen + 1,), dtype=np.int32)
        for i in range(seq_ids.shape[0]):
            for j in range(seq_ids.shape[1]):
                if tag_ids[i, j] in self._cache:
                    tags[i, j] = self._cache[tag_ids[i, j]]
                else:
                    tags[i, j, 0] = tag_ids[i, j]
                    seq = seqs[seq_ids[i, j]]
                    if len(seq) == self._taglen:
                        for k in range(self._taglen):
                            tags[i, j, k + 1] = self._alphabet_maps[k].get(self._train_alphabet[seq[k] if seq[k] < len(self._train_alphabet) else 0], 0)
                    else:
                        tags[i, j, 1:] = 0

        return tags

    def decode(self, tags):
        result = []
        for sentence in tags:
            result.append([self._train.factors[self._train.TAGS].words[tag[0]] for tag in sentence])

        return result


class DictTags:
    def __init__(self, train, regularization_weight_compositional=1., regularization_weight_whole=1.):
        self._train = train

        self._regularization_weight_compositional = regularization_weight_compositional
        self._regularization_weight_whole = regularization_weight_whole

        self._train_alphabet = train.factors[train.TAGS].alphabet

        self._taglen = len(train.factors[train.TAGS].strings[0][0])

        self._alphabet_maps = [{"<unk>": 0} for _ in range(self._taglen)]
        self._alphabets = [["<unk>"] for _ in range(self._taglen)]
        self._cache = {}
        for tag_id, tag in enumerate(train.factors[train.TAGS].words):
            if len(tag) != self._taglen: continue

            entry = np.zeros(self._taglen + 1)
            entry[0] = tag_id
            for i in range(self._taglen):
                if tag[i] not in self._alphabet_maps[i]:
                    self._alphabet_maps[i][tag[i]] = len(self._alphabets[i])
                    self._alphabets[i].append(tag[i])
                entry[i + 1] = self._alphabet_maps[i][tag[i]]
            self._cache[tag_id] = entry

    def num_tags(self):
        return [(len(self._train.factors[self._train.TAGS].words), self._regularization_weight_whole)] + \
               [(len(alphabet), self._regularization_weight_compositional) for alphabet in self._alphabets]

    def accuracy_mask(self):
        return [True] + [False] * self._taglen

    def encode(self, tag_ids, seq_ids, seqs):
        tags = np.zeros(seq_ids.shape + (self._taglen + 1,))
        for i in range(seq_ids.shape[0]):
            for j in range(seq_ids.shape[1]):
                if tag_ids[i, j] in self._cache:
                    tags[i, j] = self._cache[tag_ids[i, j]]
                else:
                    tags[i, j, 0] = tag_ids[i, j]
                    seq = seqs[seq_ids[i, j]]
                    if len(seq) == self._taglen:
                        for k in range(self._taglen):
                            tags[i, j, k + 1] = self._alphabet_maps[k].get(self._train_alphabet[seq[k] if seq[k] < len(self._train_alphabet) else 0], 0)
                    else:
                        tags[i, j, 1:] = 0

        return tags

    def decode(self, tags):
        result = []
        for sentence in tags:
            result.append([self._train.factors[self._train.TAGS].words[tag[0]] for tag in sentence])

        return result


def main():
    # Parse arguments
    parser = argparse.ArgumentParser()
    # General and training arguments
    parser.add_argument("--batch_size", default=32, type=int, help="Batch size.")
    parser.add_argument("--epochs", default=40, type=int, help="Number of epochs.")
    parser.add_argument("--threads", default=4, type=int, help="Maximum number of threads to use.")
    parser.add_argument("--name", default="", type=str, help="Any name comment.")
    parser.add_argument("--checkpoint", default="", type=str, help="Checkpoint.")
    parser.add_argument("--beta_2", default=0.99, type=float, help="Adam beta 2.")
    parser.add_argument("--learning_rate", default=0.001, type=float, help="Learning rate.")
    parser.add_argument("--drop_rate_after", default=20, type=int, help="Number of epochs after which the rate is quartered every 10 epochs.")
    parser.add_argument("--grad_clip", default=3.0, type=float, help="Gradient clipping (if set).")
    parser.add_argument("--record_trace", default=False, action="store_true", help="Record training trace as Chrome trace (load at 'chrome://tracing/').")
    parser.add_argument("--no_save_net", default=False, action="store_true", help="Skip checkoint saving (to save space when debugging).")
    parser.add_argument("--only_eval", default=False, action="store_true", help="Skip training and only evaluate once (from a checkpoint).")
    # Data and seed
    parser.add_argument("--seed", default=42, type=int, help="Random seed.")
    parser.add_argument("--data_prefix", default="lc-sense-czech-pdt-", type=str, help="Path+prefix for input files (prepended to 'dev.txt', 'train.txt' and 'test.txt'")
    parser.add_argument("--analyser", default=None, type=str, help="Analyser text file (default none).")
    parser.add_argument("--max_sentences", default=None, type=int, help="Max sentences to load (for quick testing).")
    # Dimensions and features
    parser.add_argument("--cle_dim", default=256, type=int, help="Character-level embedding dimension.")
    parser.add_argument("--rnn_cell", default="LSTM", type=str, help="RNN cell type.")
    parser.add_argument("--rnn_cell_dim", default=512, type=int, help="RNN cell dimension.")
    parser.add_argument("--rnn_layers", default=2, type=int, help="RNN layers.")
    parser.add_argument("--we_dim", default=512, type=int, help="Word embedding dimension.")
    parser.add_argument("--att_dim", default=256, type=int, help="Attention dimension.")
    parser.add_argument("--builtin_sense", default=False, action="store_true", help="Train and predict the sense as a builtin part of the lemma (use dataset without separated sense for that).")
    parser.add_argument("--tags_to_lemmas", default=False, action="store_true", help="Use tag components as a signal to the lemmatizer.")
    parser.add_argument("--separate_rnn", default=False, action="store_true", help="Use separate RNN for tags and lemmas/senses.")
    parser.add_argument("--separate_embed", default=False, action="store_true", help="Use separate embeddings for tags and lemmas/senses. Implies separate_rnn.")
    parser.add_argument("--beams", default=None, type=int, help="Use beam search with the given no of beams.")
    parser.add_argument("--loss_sense_w", default=0.1, type=float, help="Sense loss weight (if sense is separate).")
    parser.add_argument("--loss_lem_w", default=1.0, type=float, help="Lemmatization loss weight.")
    # Regularization
    parser.add_argument("--dropout", default=0.5, type=float, help="Dropout")
    parser.add_argument("--label_smoothing", default=0.1, type=float, help="Label smoothing.")
    parser.add_argument("--lem_smoothing", default=0.0, type=float, help="Lemma label smoothing.")
    parser.add_argument("--sense_smoothing", default=0.05, type=float, help="Sense label smoothing.")
    parser.add_argument("--word_dropout", default=0.25, type=float, help="Word dropout")
    parser.add_argument("--tag_signal_dropout", default=None, type=float, help="Tag signal dropout to lemmatizer")
    parser.add_argument("--tag_type", default="char", choices=["char", "dict", "whole"], help="Compositional tag type.")
    parser.add_argument("--compositional_tags_regularization", default=0.1, type=float, help="Compositional tags regularization.")
    parser.add_argument("--whole_tags_regularization", default=1.0, type=float, help="Whole tags regularization.")
    parser.add_argument("--beam_len_penalty", default=0.2, type=float, help="BeamSearch length_penalty_weight param.")

    args = parser.parse_args()
    if args.separate_embed:
        args.separate_rnn = True
    if args.only_eval:
        args.epochs = 1

    # Fix the random seed
    np.random.seed(args.seed)

    # Logdir, copy the source and log the outputs
    if not os.path.exists("logs"): os.mkdir("logs")
    basename = "LT-{}-{}-S{}".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        args.name, args.seed, )
    args.logdir = "logs/" + basename
    os.mkdir(args.logdir)
    shutil.copy(__file__, args.logdir + "/taglem.py")
    tee = Tee(args.logdir + "/log.txt")
    tee.start()
    args.realstderr = tee.stderr # A bit hacky ... (to leave progress bars out of logs)
    logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.DEBUG)

    info("Running in {} with args: {}".format(args.logdir, str(args)))
    info("Commandline: {}".format(' '.join(sys.argv)))

    # Load the data
    with log_time("load inputs"):
        args.max_dev_sentences = args.max_sentences // 5 if args.max_sentences else None
        train = morpho_dataset.MorphoDataset(args.data_prefix + "train.txt", max_sentences=args.max_sentences)
        dev = morpho_dataset.MorphoDataset(args.data_prefix + "dev.txt", train=train, shuffle_batches=False, max_sentences=args.max_dev_sentences)
        test = morpho_dataset.MorphoDataset(args.data_prefix + "test.txt", train=train, shuffle_batches=False, max_sentences=args.max_dev_sentences)
        analyser = MorphoAnalyzer(args.analyser) if args.analyser else None

    # Construct the network
    #if args.compositional_tags_regularization or args.whole_tags_regularization:
    if args.tag_type == "char":
        args.tags = CharTags(train, args.compositional_tags_regularization, args.whole_tags_regularization)
    # elif args.tag_type == "dict":
    #     args.tags = DictTags(train, args.compositional_tags_regularization, args.whole_tags_regularization)
    elif args.tag_type == "whole":
        args.tags = WholeTags(train)
    else:
        raise ValueError("Invalid tag_type")

    network = Network(threads=args.threads, seed=args.seed)
    network.construct(args, len(train.factors[train.FORMS].words), len(train.factors[train.FORMS].alphabet),
                      len(train.factors[train.LEMMAS].alphabet), args.tags.num_tags(),
                      len(train.factors[train.SENSES].words), train.factors[train.LEMMAS].alphabet_map["<bow>"],
                      train.factors[train.LEMMAS].alphabet_map["<eow>"])

    if args.checkpoint:
        network.saver.restore(network.session, args.checkpoint)

    # Train
    dev_best = 0
    for ep in range(args.epochs):
        rate = args.learning_rate
        if args.drop_rate_after and args.drop_rate_after <= ep:
            rate = args.learning_rate * 0.25 ** (1 + ((ep - args.drop_rate_after) // 10))

        if not args.only_eval:
            info("Training epoch %d with rate %f", ep, rate)
            network.train_epoch(train, args, rate=rate)

        info("Evaluating dev")
        dev_acc_tag, dev_acc_lem, dev_acc_lemsense = network.evaluate("dev", dev, args)
        info(".. epoch {} (step {}) dev accuracy: {:.2f} tag, {:.2f} lemma, {:.2f} lemma with sense".format(
            ep, network.session.run(network.global_step), 100 * dev_acc_tag, 100 * dev_acc_lem, 100 * dev_acc_lemsense))

        if dev_acc_tag + dev_acc_lemsense > dev_best or ep == args.epochs - 1:
            if not args.no_save_net and not args.only_eval: # To speed up testing / save disk space :)
                network.saver.save(network.session, "{}/checkpoint".format(args.logdir), global_step=network.global_step, write_meta_graph=False)

            for dset, name in [(dev, "dev"), (test, "test")]:
                fname = "{}/taglem_{}_ep{}.txt".format(args.logdir, name, ep)
                info("Predicting %s into %s", name, fname)
                with open(fname, "w") as ofile:
                    forms = dset.factors[dset.FORMS].strings
                    lemmas, tags = network.predict(dset, args)
                    for s in range(len(forms)):
                        for i in range(len(forms[s])):
                            print("{}\t{}\t{}".format(forms[s][i], lemmas[s][i], tags[s][i]), file=ofile)
                        print("", file=ofile)

        dev_best = max(dev_best, dev_acc_tag + dev_acc_lemsense)


if __name__ == "__main__":
    main()