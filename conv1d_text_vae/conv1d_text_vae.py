import copy
import math
import os
import re
import tempfile
import time
from typing import List, Tuple, Union

from annoy import AnnoyIndex
from gensim.models.keyedvectors import FastTextKeyedVectors
import keras.backend as K
from keras.callbacks import ModelCheckpoint, EarlyStopping, ReduceLROnPlateau
from keras import Input
from keras.layers import Conv1D, Conv2DTranspose, MaxPool1D, UpSampling1D, BatchNormalization, Dropout, Dense
from keras.layers import GRU, Flatten, Reshape, RepeatVector, Permute, Multiply, Lambda, Cropping1D, Masking
from keras.models import Model
from keras.optimizers import RMSprop, Nadam
from keras.utils import Sequence
from nltk.tokenize.nist import NISTTokenizer
import numpy as np
from scipy.spatial import distance
from scipy.sparse import csr_matrix
from sklearn.base import BaseEstimator, TransformerMixin, ClassifierMixin
from sklearn.utils.validation import check_is_fitted
from sklearn.cluster import DBSCAN


class BaseTokenizer:
    def tokenize_into_words(self, src: str) -> List[Tuple[int, int]]:
        raise NotImplemented

    @staticmethod
    def tokenize_into_characters(src: str, bounds_of_words: List[Tuple[int, int]]) -> List[str]:
        characters_list = []
        start_pos = 0
        for bounds_of_cur_word in bounds_of_words:
            if bounds_of_cur_word[0] > start_pos:
                characters_list.append('\n' if '\n' in src[start_pos:bounds_of_cur_word[0]] else ' ')
            characters_list += list(src[bounds_of_cur_word[0]:bounds_of_cur_word[1]])
            start_pos = bounds_of_cur_word[1]
        if start_pos < len(src):
            characters_list.append('\n' if '\n' in src[start_pos:] else ' ')
        return characters_list


class DefaultTokenizer(BaseTokenizer):
    def __init__(self, special_symbols: set=None):
        super().__init__()
        self.special_symbols = special_symbols
        self.tokenizer = NISTTokenizer()
        if (self.special_symbols is not None) and (len(self.special_symbols) > 0):
            re_expr = '(' + '|'.join([re.escape(cur) for cur in self.special_symbols]) + ')'
            self.re_for_special_symbols = re.compile(re_expr)
        else:
            self.re_for_special_symbols = None

    def tokenize_into_words(self, src: str) -> List[Tuple[int, int]]:
        prep = src.strip()
        if len(prep) == 0:
            return []
        if self.re_for_special_symbols is None:
            bounds_of_tokens = self.__tokenize_text(src)
        else:
            bounds_of_subphrases = []
            start_pos = 0
            for search_res in self.re_for_special_symbols.finditer(src):
                if (search_res.start() < 0) or (search_res.end() < 0):
                    break
                bounds_of_subphrases.append(('', (start_pos, search_res.start())))
                cur_symbol = src[search_res.start():search_res.end()]
                bounds_of_subphrases.append(
                    (
                        cur_symbol,
                        (search_res.start(), search_res.end())
                    )
                )
                start_pos = search_res.end()
            if start_pos < len(src):
                bounds_of_subphrases.append(('', (start_pos, len(src))))
            bounds_of_tokens = []
            for cur_subphrase in bounds_of_subphrases:
                if len(cur_subphrase[0]) == 0:
                    text = src[cur_subphrase[1][0]:cur_subphrase[1][1]]
                    bounds_of_tokens_in_text = self.__tokenize_text(text)
                    for cur_token in bounds_of_tokens_in_text:
                        bounds_of_tokens.append(
                            (
                                cur_subphrase[1][0] + cur_token[0],
                                cur_subphrase[1][0] + cur_token[1]
                            )
                        )
                else:
                    bounds_of_tokens.append(
                        (
                            cur_subphrase[1][0],
                            cur_subphrase[1][1]
                        )
                    )
        return bounds_of_tokens

    def __tokenize_text(self, src: str) -> List[Tuple[int, int]]:
        prep = src.strip()
        if len(prep) == 0:
            return []
        bounds_of_tokens = []
        end_pos = 0
        for cur_token in filter(lambda it2: len(it2) > 0,
                                map(lambda it1: it1.strip(), self.tokenizer.international_tokenize(src))):
            start_pos = src.find(cur_token, end_pos)
            if start_pos < 0:
                raise ValueError('Token `{0}` cannot be found in the text `{1}`!'.format(cur_token, src))
            end_pos = start_pos + len(cur_token)
            bounds_of_tokens.append((start_pos, end_pos))
        return bounds_of_tokens

    def __getstate__(self):
        return {'special_symbols': self.special_symbols}

    def __setstate__(self, state):
        self.special_symbols = state['special_symbols']
        if (self.special_symbols is not None) and (len(self.special_symbols) > 0):
            re_expr = '(' + '|'.join([re.escape(cur) for cur in self.special_symbols]) + ')'
            self.re_for_special_symbols = re.compile(re_expr)
        else:
            self.re_for_special_symbols = None
        self.tokenizer = NISTTokenizer()

    def __copy__(self):
        cls = self.__class__
        result = cls.__new__(cls)
        result.special_symbols = self.special_symbols
        result.tokenizer = NISTTokenizer()

    def __deepcopy__(self, memodict={}):
        cls = self.__class__
        result = cls.__new__(cls)
        result.special_symbols = copy.deepcopy(self.special_symbols)
        result.tokenizer = NISTTokenizer()


class Conv1dTextVAE(BaseEstimator, TransformerMixin, ClassifierMixin):
    SEQUENCE_BEGIN = '<BOS>'
    SEQUENCE_END = '<EOS>'

    def __init__(self, input_embeddings: FastTextKeyedVectors, output_embeddings: FastTextKeyedVectors,
                 tokenizer: BaseTokenizer=None, n_filters: Union[int, tuple]=128, kernel_size: int=3, latent_dim: int=5,
                 n_recurrent_units: int=128, input_text_size: int=None, output_text_size: int=None, batch_size: int=64,
                 max_epochs: int=100, lr: float=0.001, validation_fraction: float=0.2, use_batch_norm: bool=False,
                 use_attention: bool=True, output_onehot_size: int=None, warm_start: bool=False, verbose: bool=False):
        self.n_filters = n_filters
        self.kernel_size = kernel_size
        self.input_embeddings = input_embeddings
        self.output_embeddings = output_embeddings
        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self.lr = lr
        self.latent_dim = latent_dim
        self.warm_start = warm_start
        self.verbose = verbose
        self.input_text_size = input_text_size
        self.output_text_size = output_text_size
        self.validation_fraction = validation_fraction
        self.n_recurrent_units = n_recurrent_units
        self.tokenizer = tokenizer
        self.output_onehot_size = output_onehot_size
        self.use_batch_norm = use_batch_norm
        self.use_attention = use_attention

    def __del__(self):
        if hasattr(self, 'vae_encoder_') or hasattr(self, 'vae_decoder_') or hasattr(self, 'generator_encoder_') or \
                hasattr(self, 'generator_decoder_'):
            if hasattr(self, 'vae_encoder_'):
                del self.vae_encoder_
            if hasattr(self, 'vae_decoder_'):
                del self.vae_decoder_
            if hasattr(self, 'generator_encoder_'):
                del self.generator_encoder_
            if hasattr(self, 'generator_decoder_'):
                del self.generator_decoder_
            K.clear_session()
        if hasattr(self, 'input_embeddings'):
            del self.input_embeddings
        if hasattr(self, 'output_embeddings'):
            del self.output_embeddings
        if hasattr(self, 'tokenizer'):
            del self.tokenizer

    def fit(self, X: Union[list, tuple, np.ndarray], y: Union[list, tuple, np.ndarray]=None):
        self.check_params(**self.get_params(deep=False))
        self.check_embeddings()
        self.check_texts_param(X, 'X')
        if y is None:
            y_ = X
        else:
            self.check_texts_param(y, 'y')
            if len(y) != len(X):
                raise ValueError('Length of `X` does not equal to length of `y`! {0} != {1}.'.format(len(X), len(y)))
            y_ = y
        if self.tokenizer is None:
            self.tokenizer = DefaultTokenizer()
        n_eval_set = int(round(len(X) * self.validation_fraction))
        if n_eval_set < 1:
            raise ValueError('`validation_fraction` is too small! There are no samples for evaluation!')
        if n_eval_set >= len(X):
            raise ValueError('`validation_fraction` is too large! There are no samples for training!')
        if self.warm_start:
            self.check_is_fitted()
            true_vector_size = self.calc_vector_size(
                self.input_embeddings,
                self.tokenizer.special_symbols if hasattr(self.tokenizer, 'special_symbols') else None
            )
            if self.input_vector_size_ != true_vector_size:
                raise ValueError('Input embeddings do not correspond to the neural network!')
        X_eval = X[-n_eval_set:]
        y_eval = y_[-n_eval_set:]
        X_train = X[:-n_eval_set]
        y_train = y_[:-n_eval_set]
        max_text_size = 0
        for idx in range(len(y_)):
            bounds_of_words = self.tokenizer.tokenize_into_words(y_[idx])
            text_size = len(bounds_of_words)
            if text_size > max_text_size:
                max_text_size = text_size
        if max_text_size == 0:
            raise ValueError('The parameters `y` is wrong! All texts are empty!')
        if self.output_text_size is None:
            self.output_text_size_ = max_text_size
        else:
            self.output_text_size_ = self.output_text_size
        if hasattr(self.tokenizer, 'special_symbols'):
            if (self.tokenizer.special_symbols is None) or (len(self.tokenizer.special_symbols) == 0):
                special_symbols = None
            else:
                special_symbols = tuple(sorted(list(self.tokenizer.special_symbols)))
        else:
            special_symbols = None
        input_texts = tuple([
            Conv1dTextVAE.tokenize(cur_text, self.tokenizer.tokenize_into_words(cur_text))
            for cur_text in X_train + X_eval
        ])
        input_vocabulary, input_word_vectors = self.prepare_vocabulary_and_word_vectors(
            input_texts, self.input_embeddings, special_symbols)
        self.input_vector_size_ = input_word_vectors.shape[1]
        target_texts = []
        target_texts_by_characters = []
        for cur_text in y_train + y_eval:
            bounds_of_words = self.tokenizer.tokenize_into_words(cur_text)
            target_texts.append(Conv1dTextVAE.tokenize(cur_text, bounds_of_words))
            target_texts_by_characters.append(tuple(self.tokenizer.tokenize_into_characters(cur_text, bounds_of_words)))
        target_texts_by_characters = tuple(target_texts_by_characters)
        target_texts = tuple(target_texts)
        target_characters = set()
        self.output_text_size_in_characters_ = 0
        for cur in target_texts_by_characters:
            target_characters |= set(cur)
            n_characters = len(cur)
            if n_characters > self.output_text_size_in_characters_:
                self.output_text_size_in_characters_ = n_characters
        target_characters = sorted(list(target_characters | {self.SEQUENCE_BEGIN, self.SEQUENCE_END}))
        self.output_text_size_in_characters_ += 3
        self.target_char_index_ = dict([(char, i) for i, char in enumerate(target_characters)])
        self.reverse_target_char_index_ = dict((i, char) for char, i in self.target_char_index_.items())
        if self.output_embeddings is None:
            output_vocabulary, output_word_vectors = self.prepare_vocabulary_and_word_vectors(
                target_texts, self.input_embeddings, special_symbols, self.output_onehot_size, verbose=self.verbose)
        else:
            output_vocabulary, output_word_vectors = self.prepare_vocabulary_and_word_vectors(
                target_texts, self.output_embeddings, special_symbols, self.output_onehot_size, verbose=self.verbose)
        self.output_vector_size_ = output_word_vectors.shape[1]
        if self.warm_start:
            all_weights = self.__dump_weights(self.vae_encoder_)
            del self.vae_encoder_, self.generator_encoder_, self.generator_decoder_
            self.vae_encoder_, self.vae_decoder_, self.generator_encoder_, self.generator_decoder_, \
            vae_model_for_training, seq2seq_model_for_training = self.__create_model(
                input_vector_size=input_word_vectors.shape[1], output_vector_size=output_word_vectors.shape[1],
                warm_start=True, output_vectors=output_word_vectors
            )
            self.__load_weights(self.vae_encoder_, all_weights)
        else:
            if self.input_text_size is None:
                max_text_size = 0
                for idx in range(len(X)):
                    text_size = len(self.tokenizer.tokenize_into_words(X[idx]))
                    if text_size > max_text_size:
                        max_text_size = text_size
                if max_text_size == 0:
                    raise ValueError('The parameters `X` is wrong! All texts are empty!')
                self.input_text_size_ = max_text_size
            else:
                self.input_text_size_ = self.input_text_size
            self.vae_encoder_, self.vae_decoder_, self.generator_encoder_, self.generator_decoder_, \
            vae_model_for_training, seq2seq_model_for_training = self.__create_model(
                input_vector_size=input_word_vectors.shape[1], output_vector_size=output_word_vectors.shape[1],
                output_vectors=output_word_vectors
            )
        training_set_generator = SequenceForVAE(
            input_texts=input_texts[:len(X_train)], target_texts=target_texts[:len(y_train)], tokenizer=self.tokenizer,
            batch_size=self.batch_size, input_text_size=self.input_text_size_, output_text_size=self.output_text_size_,
            input_vocabulary=input_vocabulary, output_vocabulary=output_vocabulary,
            input_word_vectors=input_word_vectors, output_word_vectors=output_word_vectors
        )
        evaluation_set_generator = SequenceForVAE(
            input_texts=input_texts[len(X_train):], target_texts=target_texts[len(y_train):], tokenizer=self.tokenizer,
            batch_size=self.batch_size, input_text_size=self.input_text_size_, output_text_size=self.output_text_size_,
            input_vocabulary=input_vocabulary, output_vocabulary=output_vocabulary,
            input_word_vectors=input_word_vectors, output_word_vectors=output_word_vectors
        )
        callbacks = [
            EarlyStopping(patience=min(5, self.max_epochs), verbose=(1 if self.verbose else 0)),
            ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=2, verbose=(1 if self.verbose else 0))
        ]
        tmp_weights_name = self.get_temp_name()
        try:
            callbacks.append(
                ModelCheckpoint(filepath=tmp_weights_name, save_best_only=True, save_weights_only=True, verbose=(1 if self.verbose else 0))
            )
            if self.verbose:
                print('')
                print('----------------------------------------')
                print('VAE training...')
                print('----------------------------------------')
                print('')
            vae_model_for_training.fit_generator(
                generator=training_set_generator,
                epochs=self.max_epochs,
                verbose=(True if isinstance(self.verbose, int) and (self.verbose > 1) else False),
                shuffle=True,
                validation_data=evaluation_set_generator,
                callbacks=callbacks
            )
            if os.path.isfile(tmp_weights_name):
                vae_model_for_training.load_weights(tmp_weights_name)
            del callbacks
            del training_set_generator, evaluation_set_generator
        finally:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)
        if self.warm_start:
            for layer in vae_model_for_training.layers:
                layer.trainable = True
        vae_model_for_training._make_predict_function()
        training_set_generator = SequenceForSeq2Seq(
            input_texts=input_texts[:len(X_train)], target_texts=target_texts_by_characters[:len(y_train)],
            tokenizer=self.tokenizer, batch_size=self.batch_size, input_text_size=self.input_text_size_,
            output_text_size=self.output_text_size_in_characters_, input_vocabulary=input_vocabulary,
            input_word_vectors=input_word_vectors, vae=vae_model_for_training, output_char_index=self.target_char_index_
        )
        evaluation_set_generator = SequenceForSeq2Seq(
            input_texts=input_texts[len(X_train):], target_texts=target_texts_by_characters[len(y_train):],
            tokenizer=self.tokenizer, batch_size=self.batch_size, input_text_size=self.input_text_size_,
            output_text_size=self.output_text_size_in_characters_, input_vocabulary=input_vocabulary,
            input_word_vectors=input_word_vectors, vae=vae_model_for_training, output_char_index=self.target_char_index_
        )
        callbacks = [
            EarlyStopping(patience=min(5, self.max_epochs), verbose=(1 if self.verbose else 0)),
            ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=2, verbose=(1 if self.verbose else 0))
        ]
        tmp_weights_name = self.get_temp_name()
        try:
            callbacks.append(
                ModelCheckpoint(filepath=tmp_weights_name, save_best_only=True, save_weights_only=True, verbose=(1 if self.verbose else 0))
            )
            if self.verbose:
                print('')
                print('----------------------------------------')
                print('Seq2Seq training...')
                print('----------------------------------------')
                print('')
            seq2seq_model_for_training.fit_generator(
                generator=training_set_generator,
                epochs=self.max_epochs,
                verbose=(True if isinstance(self.verbose, int) and (self.verbose > 1) else False),
                shuffle=True,
                validation_data=evaluation_set_generator,
                callbacks=callbacks
            )
            if os.path.isfile(tmp_weights_name):
                seq2seq_model_for_training.load_weights(tmp_weights_name)
            del callbacks
            del training_set_generator, evaluation_set_generator
        finally:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)
        del vae_model_for_training
        del seq2seq_model_for_training
        return self

    def transform(self, X: Union[list, tuple, np.ndarray]) -> np.ndarray:
        self.check_is_fitted()
        self.check_embeddings()
        self.check_texts_param(X, 'X')
        outputs = None
        if self.tokenizer is None:
            self.tokenizer = DefaultTokenizer()
        if hasattr(self.tokenizer, 'special_symbols'):
            if self.tokenizer.special_symbols is not None:
                special_symbols = tuple(sorted(list(self.tokenizer.special_symbols)))
            else:
                special_symbols = None
        else:
            special_symbols = None
        for data_for_batch in self.texts_to_data(X, self.batch_size, self.input_text_size_, self.tokenizer,
                                                 self.input_embeddings, special_symbols):
            outputs_for_batch = self.vae_encoder_.predict_on_batch(data_for_batch)
            start_pos = 0 if outputs is None else outputs.shape[0]
            if (start_pos + outputs_for_batch.shape[0]) <= len(X):
                n = outputs_for_batch.shape[0]
            else:
                n = len(X) - start_pos
            if outputs is None:
                outputs = outputs_for_batch[:n].copy()
            else:
                outputs = np.vstack((outputs, outputs_for_batch[:n]))
        return outputs

    def predict(self, X: Union[list, tuple, np.ndarray]) -> Union[list, tuple, np.ndarray]:
        self.check_is_fitted()
        self.check_embeddings()
        self.check_texts_param(X, 'X')
        generated_texts = []
        if self.tokenizer is None:
            self.tokenizer = DefaultTokenizer()
        if hasattr(self.tokenizer, 'special_symbols'):
            if self.tokenizer.special_symbols is not None:
                special_symbols = tuple(sorted(list(self.tokenizer.special_symbols)))
            else:
                special_symbols = None
        else:
            special_symbols = None
        n_all_texts = len(X)
        start_pos = 0
        n_data_parts = 20
        data_part_size = len(X) // n_data_parts
        data_part_counter = 0
        if isinstance(self.verbose, int) and (self.verbose > 1):
            print('Prediction of texts with the VAE is started...')
        for data_for_batch in self.texts_to_data(X, self.batch_size, self.input_text_size_, self.tokenizer,
                                                 self.input_embeddings, special_symbols):
            reconstructed_word_vectors = self.postprocess_reconstructed_word_vectors(
                self.vae_decoder_.predict_on_batch(self.vae_encoder_.predict_on_batch(data_for_batch))
            )
            state_value = self.generator_encoder_.predict_on_batch(reconstructed_word_vectors)
            batch_size = data_for_batch.shape[0]
            end_pos = start_pos + data_for_batch.shape[0]
            if end_pos > n_all_texts:
                end_pos = n_all_texts
            n_texts_in_batch = end_pos - start_pos
            target_seq = np.zeros((batch_size, 1, len(self.target_char_index_)), dtype=np.float32)
            stop_conditions = []
            decoded_sentences = []
            for text_idx in range(batch_size):
                target_seq[text_idx, 0, self.target_char_index_[self.SEQUENCE_BEGIN]] = 1.0
                stop_conditions.append(False)
                decoded_sentences.append([])
            while not all(stop_conditions):
                output_tokens, state_value = self.generator_decoder_.predict_on_batch([target_seq, state_value])
                indices_of_sampled_tokens = np.argmax(output_tokens[:, -1, :], axis=1)
                for text_idx in range(batch_size):
                    if stop_conditions[text_idx]:
                        continue
                    sampled_char = self.reverse_target_char_index_[indices_of_sampled_tokens[text_idx]]
                    decoded_sentences[text_idx].append(sampled_char)
                    if (sampled_char == self.SEQUENCE_END) or \
                            (len(decoded_sentences[text_idx]) > self.output_text_size_in_characters_):
                        stop_conditions[text_idx] = True
                    for token_idx in range(len(self.target_char_index_)):
                        target_seq[text_idx][0][token_idx] = 0.0
                    target_seq[text_idx, 0, indices_of_sampled_tokens[text_idx]] = 1.0
            for text_idx in range(n_texts_in_batch):
                generated_texts.append(''.join(decoded_sentences[text_idx]))
                if data_part_size > 0:
                    if ((text_idx + start_pos + 1) % data_part_size) == 0:
                        data_part_counter += 1
                        if isinstance(self.verbose, int) and (self.verbose > 1):
                            print('{0}% of texts are processed...'.format(data_part_counter * (100 // n_data_parts)))
            start_pos += data_for_batch.shape[0]
            del data_for_batch
        if data_part_counter < n_data_parts:
            if isinstance(self.verbose, int) and (self.verbose > 1):
                print('100% of texts are processed...')
        if isinstance(self.verbose, int) and (self.verbose > 1):
            print('Prediction of texts with the VAE is finished...')
        return (np.array(generated_texts, dtype=object) if isinstance(X, np.ndarray) else (
            tuple(generated_texts) if isinstance(X, tuple) else generated_texts))

    def fit_transform(self, X, y=None, **fit_params):
        return self.fit(X, y).transform(X)

    def fit_predict(self, X, y=None, **fit_params):
        return self.fit(X, y).predict(X)

    def get_params(self, deep=True):
        params = {
            'n_filters': copy.copy(self.n_filters) if deep else self.n_filters,
            'kernel_size': self.kernel_size,
            'batch_size': self.batch_size,
            'max_epochs': self.max_epochs,
            'lr': self.lr,
            'latent_dim': self.latent_dim,
            'n_recurrent_units': self.n_recurrent_units,
            'use_batch_norm': self.use_batch_norm,
            'use_attention': self.use_attention,
            'output_onehot_size': self.output_onehot_size,
            'warm_start': self.warm_start,
            'verbose': self.verbose,
            'input_text_size': self.input_text_size,
            'output_text_size': self.output_text_size,
            'validation_fraction': self.validation_fraction,
            'tokenizer': None if self.tokenizer is None else (copy.deepcopy(self.tokenizer) if deep else self.tokenizer),
        }
        if hasattr(self, 'input_embeddings'):
            params['input_embeddings'] = self.input_embeddings
        if hasattr(self, 'output_embeddings'):
            params['output_embeddings'] = self.output_embeddings
        return params

    def set_params(self, **params):
        self.n_filters = params['n_filters']
        self.kernel_size = params['kernel_size']
        self.input_embeddings = params.get('input_embeddings', None)
        self.output_embeddings = params.get('output_embeddings', None)
        self.batch_size = params['batch_size']
        self.max_epochs = params['max_epochs']
        self.lr = params['lr']
        self.latent_dim = params['latent_dim']
        self.n_recurrent_units = params['n_recurrent_units']
        self.use_batch_norm = params['use_batch_norm']
        self.use_attention = params['use_attention']
        self.output_onehot_size = params['output_onehot_size']
        self.warm_start = params['warm_start']
        self.verbose = params['verbose']
        self.input_text_size = params['input_text_size']
        self.output_text_size = params['output_text_size']
        self.validation_fraction = params['validation_fraction']
        self.tokenizer = params['tokenizer']

    def check_is_fitted(self):
        check_is_fitted(self, ['input_text_size_', 'output_text_size_', 'input_vector_size_', 'output_vector_size_',
                               'vae_encoder_', 'vae_decoder_', 'generator_encoder_', 'generator_decoder_',
                               'output_text_size_in_characters_', 'target_char_index_', 'reverse_target_char_index_'])

    @staticmethod
    def postprocess_reconstructed_word_vectors(word_vectors: np.ndarray):
        if len(word_vectors.shape) != 3:
            raise ValueError('The `word_vectors` parameter is wrong! Expected 3-D array, got {0}-D array!'.format(
                len(word_vectors.shape)))
        prepared_word_vectors = np.zeros(
            shape=(word_vectors.shape[0], word_vectors.shape[1], word_vectors.shape[2] - 1),
            dtype=word_vectors.dtype
        )
        end_idx = np.full(shape=(word_vectors.shape[0],), fill_value=word_vectors.shape[1], dtype=np.int32)
        for sample_idx in range(word_vectors.shape[0]):
            for time_idx in range(word_vectors.shape[1]):
                best_word_idx = np.argmax(word_vectors[sample_idx][time_idx])
                if best_word_idx == (word_vectors.shape[2] - 1):
                    end_idx[sample_idx] = max(time_idx, 1)
                    break
        for sample_idx in range(word_vectors.shape[0]):
            n = end_idx[sample_idx]
            for time_idx in range(n):
                prepared_word_vectors[sample_idx][time_idx] = \
                    word_vectors[sample_idx][time_idx][:(word_vectors.shape[2] - 1)]
                vector_norm = np.linalg.norm(prepared_word_vectors[sample_idx][time_idx])
                prepared_word_vectors[sample_idx][time_idx] /= vector_norm
        return prepared_word_vectors

    def check_embeddings(self):
        if not hasattr(self, 'input_embeddings'):
            raise ValueError('The parameter `input_embeddings` is not defined!')
        if not hasattr(self, 'output_embeddings'):
            raise ValueError('The parameter `output_embeddings` is not defined!')
        if not isinstance(self.input_embeddings, FastTextKeyedVectors):
            raise ValueError('The parameter `input_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(FastTextKeyedVectors(vector_size=300, min_n=1, max_n=5)), type(self.input_embeddings)))
        if not self.output_embeddings is None:
            if not isinstance(self.output_embeddings, FastTextKeyedVectors):
                raise ValueError('The parameter `output_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                    type(FastTextKeyedVectors(vector_size=300, min_n=1, max_n=5)), type(self.output_embeddings)))

    @staticmethod
    def find_best_words(word_vector: np.ndarray, embeddings_model: FastTextKeyedVectors, n: int,
                        special_symbols: tuple=None) -> Union[List[tuple], None]:
        vector_size = Conv1dTextVAE.calc_vector_size(embeddings_model, special_symbols)
        norm_value = np.linalg.norm(word_vector[:embeddings_model.vector_size])
        if norm_value < K.epsilon():
            norm_value = 1.0
        res = embeddings_model.similar_by_vector(word_vector[:embeddings_model.vector_size] / norm_value, topn=n)
        best_vector = np.zeros((vector_size,), dtype=np.float32)
        best_vector[0:embeddings_model.vector_size] = embeddings_model[res[0][0]]
        norm_value = np.linalg.norm(best_vector)
        if norm_value > 0.0:
            best_vector /= norm_value
        end_sentence_vector = np.zeros((vector_size,), dtype=np.float32)
        end_sentence_vector[vector_size - 1] = 1.0
        unknown_word_vector = np.zeros((vector_size,), dtype=np.float32)
        unknown_word_vector[vector_size - 2] = 1.0
        if (special_symbols is not None) and (len(special_symbols) > 0):
            special_vectors = np.zeros((len(special_symbols), vector_size), dtype=np.float32)
            distance_to_special_vectors = np.zeros((len(special_symbols),), dtype=np.float32)
            for special_idx in range(len(special_symbols)):
                special_vectors[special_idx][embeddings_model.vector_size + special_idx] = 1.0
                distance_to_special_vectors[special_idx] = distance.cosine(word_vector, special_vectors[special_idx])
            special_idx = int(distance_to_special_vectors.argmin())
        else:
            special_idx = -1
            distance_to_special_vectors = None
        distance_to_end_vector = distance.cosine(word_vector, end_sentence_vector)
        distance_to_unknown_word = distance.cosine(word_vector, unknown_word_vector)
        distance_to_best_word = distance.cosine(word_vector, best_vector)
        if distance_to_end_vector < distance_to_unknown_word:
            if distance_to_end_vector < distance_to_best_word:
                if special_idx >= 0:
                    if distance_to_end_vector < distance_to_special_vectors[special_idx]:
                        res = None
                    else:
                        res = [(special_symbols[special_idx], distance_to_special_vectors[special_idx])]
                else:
                    res = None
        else:
            if distance_to_unknown_word < distance_to_best_word:
                if special_idx >= 0:
                    if distance_to_unknown_word < distance_to_special_vectors[special_idx]:
                        res = []
                    else:
                        res = [(special_symbols[special_idx], distance_to_special_vectors[special_idx])]
                else:
                    res = []
            else:
                if special_idx >= 0:
                    if distance_to_special_vectors[special_idx] < distance_to_best_word:
                        res = [(special_symbols[special_idx], distance_to_special_vectors[special_idx])]
                else:
                    vectors_of_similar_words = np.zeros((len(res), vector_size), dtype=np.float32)
                    for idx in range(len(res)):
                        vectors_of_similar_words[idx, 0:embeddings_model.vector_size] = embeddings_model[res[idx][0]]
                        norm_value = np.linalg.norm(vectors_of_similar_words[idx])
                        if norm_value > 0.0:
                            vectors_of_similar_words[idx] /= norm_value
                    res = [(res[idx][0], distance.cosine(word_vector, vectors_of_similar_words[idx]))
                           for idx in range(len(res))]
                    res.sort(key=lambda it: (it[1], it[0]))
        return res

    @staticmethod
    def find_best_texts(variants_of_text: List[tuple], ntop: int) -> List[str]:
        used_variants = []
        variants_and_distances = []
        new_variant = []
        for word_idx in range(len(variants_of_text)):
            variants_of_word = variants_of_text[word_idx]
            new_variant.append(variants_of_word[0][0])
            for variant_idx in range(1, len(variants_of_word)):
                variants_and_distances.append(((word_idx, variant_idx), variants_of_word[variant_idx][1]))
        used_variants.append(' '.join(new_variant))
        variants_and_distances.sort(key=lambda it: (it[1], it[0][0], it[0][1]))
        for variant_idx in range(min(ntop - 1, len(variants_and_distances))):
            word_idx = variants_and_distances[variant_idx][0][0]
            variants_of_word = variants_of_text[word_idx]
            best_variant_idx = variants_and_distances[variant_idx][0][1]
            new_variant[word_idx] = variants_of_word[best_variant_idx][0]
            used_variants.append(' '.join(new_variant))
        return used_variants

    @staticmethod
    def copy_embeddings(src: FastTextKeyedVectors) -> FastTextKeyedVectors:
        tmp_fasttext_name = Conv1dTextVAE.get_temp_name()
        try:
            src.save(tmp_fasttext_name)
            res = FastTextKeyedVectors.load(tmp_fasttext_name)
        finally:
            Conv1dTextVAE.remove_fasttext_files(tmp_fasttext_name)
        return res

    @staticmethod
    def check_texts_param(param_value: Union[list, tuple, np.ndarray], param_name: str):
        if (not isinstance(param_value, list)) and (not isinstance(param_value, tuple)) and \
                (not isinstance(param_value, np.ndarray)):
            raise ValueError('The parameter `{0}` is wrong! '
                             'Expected `{1}`, `{2}` or 1-D `{3}`, got `{4}`.'.format(
                param_name, type([1, 2]), type((1, 2)),type(np.array([1, 2])), type(param_value)))
        if isinstance(param_value, np.ndarray):
            if len(param_value.shape) != 1:
                raise ValueError('The parameter `{0}` is wrong! Expected 1-D array, got {1}-D array.'.format(
                    param_name, len(param_value.shape)))
        for idx in range(len(param_value)):
            if (not hasattr(param_value[idx], 'split')) or (not hasattr(param_value[idx], 'strip')):
                raise ValueError('Item {0} of the parameter `{1}` is wrong! '
                                 'This item is not string!'.format(idx, param_name))

    @staticmethod
    def check_params(**params):
        if 'warm_start' not in params:
            raise ValueError('The parameter `warm_start` is not defined!')
        if (not isinstance(params['warm_start'], bool)) and (not isinstance(params['warm_start'], int)):
            raise ValueError('The parameter `warm_start` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(True), type(params['warm_start'])))
        if 'use_batch_norm' not in params:
            raise ValueError('The parameter `use_batch_norm` is not defined!')
        if (not isinstance(params['use_batch_norm'], bool)) and (not isinstance(params['use_batch_norm'], int)):
            raise ValueError('The parameter `use_batch_norm` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(True), type(params['use_batch_norm'])))
        if 'use_attention' not in params:
            raise ValueError('The parameter `use_attention` is not defined!')
        if (not isinstance(params['use_attention'], bool)) and (not isinstance(params['use_attention'], int)):
            raise ValueError('The parameter `use_attention` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(True), type(params['use_attention'])))
        if 'verbose' not in params:
            raise ValueError('The parameter `verbose` is not defined!')
        if (not isinstance(params['verbose'], bool)) and (not isinstance(params['verbose'], int)):
            raise ValueError('The parameter `verbose` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(True), type(params['verbose'])))
        if 'batch_size' not in params:
            raise ValueError('The parameter `batch_size` is not defined!')
        if not isinstance(params['batch_size'], int):
            raise ValueError('The parameter `batch_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['batch_size'])))
        if params['batch_size'] <= 0:
            raise ValueError('The parameter `batch_size` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['batch_size']))
        if 'max_epochs' not in params:
            raise ValueError('The parameter `max_epochs` is not defined!')
        if not isinstance(params['max_epochs'], int):
            raise ValueError('The parameter `max_epochs` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['max_epochs'])))
        if params['max_epochs'] <= 0:
            raise ValueError('The parameter `max_epochs` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['max_epochs']))
        if 'latent_dim' not in params:
            raise ValueError('The parameter `latent_dim` is not defined!')
        if not isinstance(params['latent_dim'], int):
            raise ValueError('The parameter `latent_dim` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['latent_dim'])))
        if params['latent_dim'] <= 0:
            raise ValueError('The parameter `latent_dim` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['latent_dim']))
        if 'input_text_size' not in params:
            raise ValueError('The parameter `input_text_size` is not defined!')
        if params['input_text_size'] is not None:
            if not isinstance(params['input_text_size'], int):
                raise ValueError('The parameter `input_text_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                    type(10), type(params['input_text_size'])))
            if params['input_text_size'] <= 0:
                raise ValueError('The parameter `input_text_size` is wrong! Expected a positive value, '
                                 'but {0} is not positive.'.format(params['input_text_size']))
        if 'output_text_size' not in params:
            raise ValueError('The parameter `output_text_size` is not defined!')
        if params['output_text_size'] is not None:
            if not isinstance(params['output_text_size'], int):
                raise ValueError('The parameter `output_text_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                    type(10), type(params['output_text_size'])))
            if params['output_text_size'] <= 0:
                raise ValueError('The parameter `output_text_size` is wrong! Expected a positive value, '
                                 'but {0} is not positive.'.format(params['output_text_size']))
        if 'output_onehot_size' not in params:
            raise ValueError('The parameter `output_onehot_size` is not defined!')
        if params['output_onehot_size'] is not None:
            if not isinstance(params['output_onehot_size'], int):
                raise ValueError('The parameter `output_onehot_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                    type(10), type(params['output_onehot_size'])))
            if params['output_onehot_size'] <= 0:
                raise ValueError('The parameter `output_onehot_size` is wrong! Expected a positive value, '
                                 'but {0} is not positive.'.format(params['output_onehot_size']))
        if 'n_filters' not in params:
            raise ValueError('The parameter `n_filters` is not defined!')
        if not isinstance(params['n_filters'], int) and (not isinstance(params['n_filters'], tuple)):
            raise ValueError('The parameter `n_filters` is wrong! Expected `{0}` or `{1}`, got `{2}`.'.format(
                type(10), type((1, 2)), type(params['n_filters'])))
        if isinstance(params['n_filters'], int):
            if params['n_filters'] <= 0:
                raise ValueError('The parameter `n_filters` is wrong! Expected a positive value, '
                                 'but {0} is not positive.'.format(params['n_filters']))
        else:
            if len(params['n_filters']) < 1:
                raise ValueError('The parameter `n_filters` is wrong! Expected a nonempty sequence of integers.')
            for idx in range(len(params['n_filters'])):
                if params['n_filters'][idx] <= 0:
                    raise ValueError('Item {0} of the parameter `n_filters` is wrong! Expected a positive value, '
                                     'but {1} is not positive.'.format(idx, params['n_filters'][idx]))
        if 'kernel_size' not in params:
            raise ValueError('The parameter `kernel_size` is not defined!')
        if not isinstance(params['kernel_size'], int):
            raise ValueError('The parameter `kernel_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['kernel_size'])))
        if params['kernel_size'] <= 0:
            raise ValueError('The parameter `kernel_size` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['kernel_size']))
        if 'n_recurrent_units' not in params:
            raise ValueError('The parameter `n_recurrent_units` is not defined!')
        if not isinstance(params['n_recurrent_units'], int):
            raise ValueError('The parameter `n_recurrent_units` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['n_recurrent_units'])))
        if params['n_recurrent_units'] <= 0:
            raise ValueError('The parameter `n_recurrent_units` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['n_recurrent_units']))
        if 'validation_fraction' not in params:
            raise ValueError('The parameter `validation_fraction` is not defined!')
        if not isinstance(params['validation_fraction'], float):
            raise ValueError('The parameter `validation_fraction` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10.5), type(params['validation_fraction'])))
        if (params['validation_fraction'] <= 0) or (params['validation_fraction'] >= 1.0):
            raise ValueError('The parameter `validation_fraction` is wrong! Expected a positive value between 0.0 and '
                             '1.0, but {0} does not correspond to this condition.'.format(
                Conv1dTextVAE.float_to_string(params['validation_fraction'])))
        if 'lr' not in params:
            raise ValueError('The parameter `lr` is not defined!')
        if not isinstance(params['lr'], float):
            raise ValueError('The parameter `lr` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10.5), type(params['lr'])))
        if params['lr'] <= 0:
            raise ValueError('The parameter `lr` is wrong! Expected a positive value, but {0} is not positive.'.format(
                params['lr']))

    @staticmethod
    def calc_vector_size(embeddings: FastTextKeyedVectors, special_symbols: Union[tuple, set, None]):
        vector_size = embeddings.vector_size + 2
        if special_symbols is not None:
            vector_size += len(special_symbols)
        return vector_size

    @staticmethod
    def tokenize(src: str, bounds_of_words: List[Tuple[int, int]]) -> tuple:
        return tuple(
            filter(
                lambda it2: len(it2) > 0, map(lambda it1: src[it1[0]:it1[1]].lower(), bounds_of_words)
            )
        )

    @staticmethod
    def texts_to_data(input_texts: Union[list, tuple, np.ndarray], batch_size: int, max_text_size: int,
                      tokenizer: BaseTokenizer, fasttext_model: FastTextKeyedVectors, special_symbols: tuple=None):
        n_batches = int(math.ceil(len(input_texts) / batch_size))
        vector_size = Conv1dTextVAE.calc_vector_size(fasttext_model, special_symbols)
        for batch_ind in range(n_batches):
            input_data = np.zeros((batch_size, max_text_size, vector_size), dtype=np.float32)
            start_pos = batch_ind * batch_size
            end_pos = start_pos + batch_size
            for src_text_idx in range(start_pos, end_pos):
                prep_text_idx = src_text_idx
                if src_text_idx >= len(input_texts):
                    prep_text_idx = len(input_texts) - 1
                input_text = input_texts[prep_text_idx]
                bounds_of_input_words = tokenizer.tokenize_into_words(input_text)
                words_of_input_text = Conv1dTextVAE.tokenize(input_text, bounds_of_input_words)
                for time_idx, token in enumerate(words_of_input_text):
                    if time_idx >= max_text_size:
                        break
                    if (special_symbols is not None) and (token in special_symbols):
                        input_data[src_text_idx - start_pos, time_idx,
                                   fasttext_model.vector_size + special_symbols.index(token)] = 1.0
                    else:
                        try:
                            word_vector = fasttext_model[token]
                        except:
                            word_vector = None
                        if word_vector is None:
                            input_data[src_text_idx - start_pos, time_idx, vector_size - 2] = 1.0
                        else:
                            vector_norm = np.linalg.norm(word_vector)
                            if vector_norm < K.epsilon():
                                vector_norm = 1.0
                            input_data[src_text_idx - start_pos, time_idx, 0:fasttext_model.vector_size] = \
                                word_vector / vector_norm
                time_idx = len(words_of_input_text)
                if time_idx < max_text_size:
                    input_data[src_text_idx - start_pos, time_idx, vector_size - 1] = 1.0
            yield input_data

    @staticmethod
    def get_temp_name():
        fp = tempfile.NamedTemporaryFile(delete=True)
        file_name = fp.name
        fp.close()
        del fp
        return file_name

    @staticmethod
    def remove_fasttext_files(file_name: str):
        if os.path.isfile(file_name):
            os.remove(file_name)
        dir_name = os.path.dirname(file_name)
        base_name = os.path.basename(file_name)
        for cur in filter(lambda it: it.startswith(base_name) and it.endswith('.npy'), os.listdir(dir_name)):
            prep = os.path.join(dir_name, cur)
            if os.path.isfile(prep):
                os.remove(prep)

    @staticmethod
    def float_to_string(value: float, precision: int = 6) -> str:
        if not isinstance(value, float):
            return str(value)
        res = '{0:.{1}f}'.format(value, precision)
        n = len(res)
        start_idx = 0
        while start_idx < n:
            if res[start_idx] != '0':
                break
            start_idx += 1
        if start_idx >= n:
            return '0'
        if res[start_idx] == '.':
            if start_idx == 0:
                res = '0' + res
                n += 1
            else:
                start_idx -= 1
        end_idx = n - 1
        while end_idx > start_idx:
            if res[end_idx] != '0':
                break
            end_idx -= 1
        if res[end_idx] != '.':
            end_idx += 1
        return res[start_idx:end_idx]

    @staticmethod
    def quantize_word_vectors(word_vectors: np.ndarray, max_vocabulary_size: int,
                              verbose: bool) -> Tuple[list, np.ndarray]:
        if verbose:
            print('')
            print('----------------------------------------')
            print('Calculation of neighbourhood matrix is started...')
            print('----------------------------------------')
        word_vec_index = AnnoyIndex(word_vectors.shape[1])
        for sample_idx in range(word_vectors.shape[0]):
            word_vec_index.add_item(sample_idx, word_vectors[sample_idx])
        word_vec_index.build(max(10, int(round(np.sqrt(word_vectors.shape[0])))))
        if verbose:
            print('AnnoyIndex has been built...')
        n_max_neighbours = min(3 * max(1, int(word_vectors.shape[0] // max_vocabulary_size)), word_vectors.shape[0] - 1)
        all_data = np.empty((word_vectors.shape[0] * n_max_neighbours,), dtype=np.float32)
        all_rows = np.empty(all_data.shape, dtype=np.int32)
        all_cols = np.empty(all_data.shape, dtype=np.int32)
        cell_idx = 0
        n_data_parts = 10
        data_part_size = word_vectors.shape[0] // n_data_parts
        data_part_counter = 0
        start_time = time.time()
        for sample_idx in range(word_vectors.shape[0]):
            neighbours, distances = word_vec_index.get_nns_by_item(
                sample_idx, n_max_neighbours, search_k=-1,
                include_distances=True
            )
            for neighbour_idx in range(n_max_neighbours):
                all_data[cell_idx] = distances[neighbour_idx]
                all_rows[cell_idx] = sample_idx
                all_cols[cell_idx] = neighbours[neighbour_idx]
                cell_idx += 1
            if data_part_size > 0:
                if ((sample_idx + 1) % data_part_size) == 0:
                    data_part_counter += 1
                    if verbose:
                        print('{0:.3f} seconds:\t{1}% of vectors has been processed...'.format(
                            time.time() - start_time, data_part_counter * (100 // n_data_parts)))
        if data_part_counter < n_data_parts:
            if verbose:
                print('{0:.3f} seconds:\t100% of vectors has been processed...'.format(time.time() - start_time))
        del word_vec_index
        if verbose:
            print('Number of all elements is {0}.'.format(word_vectors.shape[0] * word_vectors.shape[0]))
            print('Part of nonzero elements is {0:.3%}.'.format(
                len(all_data) / float(word_vectors.shape[0] * word_vectors.shape[0])))
        neighbourhood_matrix = csr_matrix((all_data, (all_rows, all_cols)),
                                          shape=(word_vectors.shape[0], word_vectors.shape[0]))
        max_distance = np.mean(all_data)
        del all_data, all_rows, all_cols
        clustering = DBSCAN(n_jobs=1, min_samples=max(1, int(word_vectors.shape[0] // (max_vocabulary_size * 4))),
                            metric='precomputed', eps=max_distance)
        if verbose:
            print('')
            print('----------------------------------------')
            print('DBSCAN clustering with scikit-learn is started...')
            print('----------------------------------------')
            print('n_samples = {0}'.format(word_vectors.shape[0]))
            print('DBSCAN.min_samples = {0}'.format(clustering.min_samples))
            print('DBSCAN.eps = {0}'.format(clustering.eps))
        predicted = clustering.fit_predict(neighbourhood_matrix)
        n_clusters = len(set(predicted.tolist()) - {-1})
        n_clusters_with_noise = n_clusters + sum(map(lambda it: 1 if predicted[it] < 0 else 0, range(len(predicted))))
        if verbose:
            print('DBSCAN training is finished...')
            print('Number of "good" clusters is {0}.'.format(n_clusters))
            print('Number of all clusters (including noisy data) is {0}.'.format(n_clusters_with_noise))
            print('')
            print('Quantization with DBSCAN is started...')
        cluster_centers = np.zeros((n_clusters_with_noise, word_vectors.shape[1]), dtype=np.float32)
        frequencies = np.zeros((n_clusters_with_noise,), dtype=np.int32)
        n_noise_samples = 0
        word_clusters = list()
        for sample_idx in range(len(predicted)):
            cluster_idx = predicted[sample_idx]
            if cluster_idx < 0:
                cluster_idx = n_clusters + n_noise_samples
                n_noise_samples += 1
            cluster_centers[cluster_idx] += word_vectors[sample_idx]
            word_clusters.append(cluster_idx)
            frequencies[cluster_idx] += 1
        for cluster_idx in range(cluster_centers.shape[0]):
            cluster_centers[cluster_idx] /= frequencies[cluster_idx]
            vector_norm = np.linalg.norm(cluster_centers[cluster_idx])
            cluster_centers[cluster_idx] /= vector_norm
        del word_vectors
        del clustering
        del frequencies, predicted
        if verbose:
            print('Quantization with DBSCAN is finished...')
            print('')
        return word_clusters, cluster_centers

    @staticmethod
    def get_vocabulary_and_word_vectors_from_fasttext(
            all_texts, fasttext_vectors: FastTextKeyedVectors, special_symbols: Union[tuple, None],
            max_vocabulary_size: int=None, verbose: bool=False) -> Tuple[dict, np.ndarray]:
        vocabulary = dict()
        word_idx = 0
        for cur_text in all_texts:
            for cur_word in filter(lambda it: len(it) > 0, cur_text):
                if special_symbols is not None:
                    if cur_word in special_symbols:
                        continue
                try:
                    word_vector = fasttext_vectors[cur_word]
                except:
                    word_vector = None
                if (cur_word not in vocabulary) and (word_vector is not None):
                    vector_norm = np.linalg.norm(word_vector)
                    if vector_norm > K.epsilon():
                        vocabulary[cur_word] = word_idx
                        word_idx += 1
        word_vectors = np.zeros((word_idx, fasttext_vectors.vector_size), dtype=np.float32)
        for cur_word in vocabulary:
            word_idx = vocabulary[cur_word]
            word_vector = fasttext_vectors[cur_word]
            vector_norm = np.linalg.norm(word_vector)
            word_vectors[word_idx] = word_vector / vector_norm
        if (max_vocabulary_size is not None) and (max_vocabulary_size < word_vectors.shape[0]):
            word_clusters, word_vectors = Conv1dTextVAE.quantize_word_vectors(word_vectors, max_vocabulary_size,
                                                                              verbose)
            for cur_word in vocabulary:
                vocabulary[cur_word] = word_clusters[vocabulary[cur_word]]
        return vocabulary, word_vectors

    @staticmethod
    def prepare_vocabulary_and_word_vectors(all_texts, fasttext_vectors: FastTextKeyedVectors,
                                            special_symbols: Union[tuple, None], max_vocabulary_size: int=None,
                                            verbose: bool=False) -> Tuple[dict, np.ndarray]:
        src_fasttext_vocabulary, src_fasttext_vectors = Conv1dTextVAE.get_vocabulary_and_word_vectors_from_fasttext(
            all_texts, fasttext_vectors, special_symbols, max_vocabulary_size, verbose
        )
        vector_size = Conv1dTextVAE.calc_vector_size(fasttext_vectors, special_symbols)
        vocabulary = dict()
        word_vectors = np.zeros(
            (src_fasttext_vectors.shape[0] + (0 if special_symbols is None else len(special_symbols)) + 2, vector_size),
            dtype=np.float32
        )
        word_vectors[0:src_fasttext_vectors.shape[0], 0:fasttext_vectors.vector_size] = src_fasttext_vectors
        if (special_symbols is not None) and (len(special_symbols) > 0):
            for word_idx in range(len(special_symbols)):
                word_vectors[src_fasttext_vectors.shape[0] + 1 + word_idx,
                             fasttext_vectors.vector_size + 1 + word_idx] = 1.0
        word_vectors[src_fasttext_vectors.shape[0], fasttext_vectors.vector_size] = 1.0
        word_vectors[word_vectors.shape[0] - 1, vector_size - 1] = 1.0
        for cur_text in all_texts:
            for cur_word in filter(lambda it: len(it) > 0, cur_text):
                if cur_word not in vocabulary:
                    if (special_symbols is not None) and (len(special_symbols) > 0):
                        if cur_word in special_symbols:
                            vocabulary[cur_word] = src_fasttext_vectors.shape[0] + 1 + special_symbols.index(cur_word)
                        else:
                            if cur_word in src_fasttext_vocabulary:
                                vocabulary[cur_word] = src_fasttext_vocabulary[cur_word]
                            else:
                                vocabulary[cur_word] = src_fasttext_vectors.shape[0]
                    else:
                        if cur_word in src_fasttext_vocabulary:
                            vocabulary[cur_word] = src_fasttext_vocabulary[cur_word]
                        else:
                            vocabulary[cur_word] = src_fasttext_vectors.shape[0]
        vocabulary[''] = word_vectors.shape[0] - 1
        del src_fasttext_vectors
        del src_fasttext_vocabulary
        return vocabulary, word_vectors

    def __load_weights(self, model: Model, weights_as_bytes: Union[bytearray, bytes]):
        if (not isinstance(weights_as_bytes, bytearray)) and (not isinstance(weights_as_bytes, bytes)):
            raise ValueError('The `weights_as_bytes` must be an array of bytes, not `{0}`!'.format(
                type(weights_as_bytes)))
        tmp_weights_name = self.get_temp_name()
        try:
            with open(tmp_weights_name, 'wb') as fp:
                fp.write(weights_as_bytes)
            model.load_weights(tmp_weights_name)
            os.remove(tmp_weights_name)
        finally:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)

    def __dump_weights(self, model: Model):
        self.check_is_fitted()
        tmp_weights_name = self.get_temp_name()
        try:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)
            model.save_weights(tmp_weights_name)
            with open(tmp_weights_name, 'rb') as fp:
                weights_of_model = fp.read()
            os.remove(tmp_weights_name)
        finally:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)
        return weights_of_model

    def __getstate__(self):
        state = self.get_params(deep=False)
        del state['input_embeddings']
        del state['output_embeddings']
        params_after_fitting = ['input_text_size_', 'output_text_size_', 'input_vector_size_', 'output_vector_size_',
                               'vae_encoder_', 'vae_decoder_', 'generator_encoder_', 'generator_decoder_',
                               'output_text_size_in_characters_', 'target_char_index_', 'reverse_target_char_index_']
        if all(map(lambda it: hasattr(self, it), params_after_fitting)):
            state['input_text_size_'] = self.input_text_size_
            state['output_text_size_'] = self.output_text_size_
            state['input_vector_size_'] = self.input_vector_size_
            state['output_vector_size_'] = self.output_vector_size_
            state['output_text_size_in_characters_'] = self.output_text_size_in_characters_
            state['target_char_index_'] = copy.deepcopy(self.target_char_index_)
            state['reverse_target_char_index_'] = copy.deepcopy(self.reverse_target_char_index_)
            state['weights_'] = {
                'vae_encoder': self.__dump_weights(self.vae_encoder_),
                'vae_decoder': self.__dump_weights(self.vae_decoder_),
                'seq2seq_encoder': self.__dump_weights(self.generator_encoder_),
                'seq2seq_decoder': self.__dump_weights(self.generator_decoder_)
            }
        return state

    def __setstate__(self, state):
        if not isinstance(state, dict):
            raise ValueError('`state` is wrong! Expected {0}.'.format(type({0: 1})))
        state['input_embeddings'] = None
        state['output_embeddings'] = None
        self.check_params(**state)
        if hasattr(self, 'vae_encoder_') or hasattr(self, 'vae_decoder_') or hasattr(self, 'generator_encoder_') or \
                hasattr(self, 'generator_decoder_'):
            if hasattr(self, 'vae_encoder_'):
                del self.vae_encoder_
            if hasattr(self, 'vae_decoder_'):
                del self.vae_decoder_
            if hasattr(self, 'generator_encoder_'):
                del self.generator_encoder_
            if hasattr(self, 'generator_decoder_'):
                del self.generator_decoder_
            K.clear_session()
        is_fitted = all(map(
            lambda it: it in state,
            ['input_text_size_', 'output_text_size_', 'input_vector_size_', 'output_vector_size_', 'weights_',
             'output_text_size_in_characters_', 'target_char_index_', 'reverse_target_char_index_']
        ))
        self.set_params(**state)
        if is_fitted:
            if not isinstance(state['weights_'], dict):
                raise ValueError('Weights are wrong! Expected `{0}`, got `{1}`.'.format(type({'a': 1, 'b': 2}),
                                                                                        type(state['weights_'])))
            if 'vae_encoder' not in state['weights_']:
                raise ValueError('Weights are wrong! The key `vae_encoder` is not found.')
            if 'vae_decoder' not in state['weights_']:
                raise ValueError('Weights are wrong! The key `vae_decoder` is not found.')
            if 'seq2seq_encoder' not in state['weights_']:
                raise ValueError('Weights are wrong! The key `seq2seq_encoder` is not found.')
            if 'seq2seq_decoder' not in state['weights_']:
                raise ValueError('Weights are wrong! The key `seq2seq_decoder` is not found.')
            self.input_text_size_ = state['input_text_size_']
            self.output_text_size_ = state['output_text_size_']
            self.output_text_size_in_characters_ = state['output_text_size_in_characters_']
            self.target_char_index_ = copy.deepcopy(state['target_char_index_'])
            self.reverse_target_char_index_ = copy.deepcopy(state['reverse_target_char_index_'])
            self.input_vector_size_ = state['input_vector_size_']
            self.output_vector_size_ = state['output_vector_size_']
            self.vae_encoder_, self.vae_decoder_, self.generator_encoder_, self.generator_decoder_, _, _ = \
                self.__create_model(input_vector_size=self.input_vector_size_,
                                    output_vector_size=self.output_vector_size_)
            self.__load_weights(self.vae_encoder_, state['weights_']['vae_encoder'])
            self.__load_weights(self.vae_decoder_, state['weights_']['vae_decoder'])
            self.__load_weights(self.generator_encoder_, state['weights_']['seq2seq_encoder'])
            self.__load_weights(self.generator_decoder_, state['weights_']['seq2seq_decoder'])

    def __create_model(self, input_vector_size: int, output_vector_size: int, output_vectors: np.ndarray=None,
                       warm_start: bool=False) -> Tuple[Model, Model, Model, Model, Model, Model]:

        def sampling(args):
            z_mean_, z_log_var_ = args
            epsilon = K.random_normal(shape=(K.shape(z_mean_)[0], self.latent_dim), mean=0.0, stddev=1.0)
            return z_mean_ + K.exp(z_log_var_) * epsilon

        def vae_loss(y_true, y_pred):
            y_pred_ = K.softmax(
                (1.0 / tau) * K.dot(K.l2_normalize(y_pred, axis=-1), weights_of_layer_for_reconstruction),
                axis=-1
            )
            xent_loss = K.categorical_crossentropy(target=y_true, output=y_pred_, axis=-1)
            mask = K.cast(K.any(K.not_equal(y_true, 0.0), axis=-1), dtype=K.dtype(xent_loss))
            xent_loss = xent_loss * mask
            xent_loss = K.sum(xent_loss, axis=-1) / K.sum(mask, axis=-1)
            kl_loss = K.mean(1 + z_log_var - K.square(z_mean) - K.exp(z_log_var), axis=-1)
            return xent_loss - kl_loss

        def attention_for_seq2seq_encoder(inputs):
            a = Permute((2, 1), name='attention_aux1')(inputs)
            a = Reshape((output_vector_size - 1, self.output_text_size_), name='attention_aux2')(a)
            a = Dense(self.output_text_size_, use_bias=False, activation='softmax', name='attention_aux3')(a)
            a = Lambda(lambda x: K.mean(x, axis=1), name='attention_dim_reduction')(a)
            a = RepeatVector(output_vector_size - 1, name='attention_aux4')(a)
            a_probs = Permute((2, 1), name='attention_vec')(a)
            output_attention_mul = Multiply(name='attention_mul')([inputs, a_probs])
            return output_attention_mul

        def Conv1DTranspose(input_tensor, filters, kernel_size, strides=1, padding='same', activation='tanh',
                            name: str = "", trainable: bool = True):
            x = Lambda(lambda x: K.expand_dims(x, axis=2), name=name + '_deconv1d_part1')(input_tensor)
            x = Conv2DTranspose(filters=filters, kernel_size=(kernel_size, 1),
                                activation=activation, strides=(strides, 1), padding=padding,
                                name=name + '_deconv1d_part2', trainable=trainable)(x)
            x = Lambda(lambda x: K.squeeze(x, axis=2), name=name + '_deconv1d_part3')(x)
            return x

        encoder_input = Input(shape=(self.input_text_size_, input_vector_size), dtype='float32',
                              name='encoder_embeddings')
        n_filters = self.n_filters if isinstance(self.n_filters, int) else self.n_filters[0]
        encoder = Conv1D(filters=n_filters, kernel_size=self.kernel_size, activation='relu',
                         padding='valid', name='encoder_conv1d_1', trainable=(not warm_start))(encoder_input)
        if self.use_batch_norm:
            encoder = BatchNormalization(name='encoder_batchnorm_1')(encoder)
        encoder = MaxPool1D(pool_size=2, name='encoder_pool1d_1')(encoder)
        if isinstance(self.n_filters, tuple):
            layer_counter = 2
            for n_filters in self.n_filters[1:]:
                encoder = Conv1D(filters=n_filters, kernel_size=self.kernel_size, activation='relu',
                                 padding='valid', name='encoder_conv1d_{0}'.format(layer_counter),
                                 trainable=(not warm_start))(encoder)
                if self.use_batch_norm:
                    encoder = BatchNormalization(name='encoder_batchnorm_{0}'.format(layer_counter))(encoder)
                encoder = MaxPool1D(pool_size=2, name='encoder_pool1d_{0}'.format(layer_counter))(encoder)
                layer_counter += 1
        encoder = Flatten(name='encoder_flatten')(encoder)
        encoder = Dropout(0.3, name='encoder_dropout')(encoder)
        z_mean = Dense(self.latent_dim, name='z_mean', trainable=(not warm_start))(encoder)
        z_log_var = Dense(self.latent_dim, name='z_log_var', trainable=(not warm_start))(encoder)
        z = Lambda(sampling, name='z')([z_mean, z_log_var])
        n_times_of_decoder = int(math.ceil((self.output_text_size_ - self.kernel_size + 1) / 2.0))
        if isinstance(self.n_filters, tuple):
            for _ in range(len(self.n_filters) - 1):
                n_times_of_decoder = int(math.ceil((n_times_of_decoder - self.kernel_size + 1) / 2.0))
        width = max(10, int(math.ceil(self.latent_dim / float(n_times_of_decoder))))
        deconv_decoder_input = Input(shape=(self.latent_dim,), dtype='float32', name='deconv_decoder_input')
        deconv_decoder = Dense(n_times_of_decoder * width, activation='relu', name='deconv_decoder_dense')(
            Dropout(0.3, name='deconv_decoder_dropout')(deconv_decoder_input))
        deconv_decoder = Reshape((n_times_of_decoder, width), name='deconv_decoder_reshape')(deconv_decoder)
        n_filters = self.n_filters if isinstance(self.n_filters, int) else self.n_filters[-1]
        deconv_decoder = Conv1DTranspose(deconv_decoder, filters=n_filters, kernel_size=self.kernel_size,
                                         activation='relu', name='deconv_decoder_1', trainable=True, padding='valid')
        if self.use_batch_norm:
            deconv_decoder = BatchNormalization(name='deconv_decoder_batchnorm_1')(deconv_decoder)
        deconv_decoder = UpSampling1D(size=2, name='deconv_decoder_upsampling_1')(deconv_decoder)
        if isinstance(self.n_filters, tuple):
            layer_counter = 2
            idx = list(range(len(self.n_filters) - 1))
            idx.reverse()
            for n_filters in map(lambda it: self.n_filters[it], idx):
                deconv_decoder = Conv1DTranspose(deconv_decoder, n_filters, kernel_size=self.kernel_size,
                                                 activation='relu', name='deconv_decoder_{0}'.format(layer_counter),
                                                 trainable=True, padding='valid')
                if self.use_batch_norm:
                    deconv_decoder = BatchNormalization(name='deconv_decoder_batchnorm_{0}'.format(layer_counter))(
                        deconv_decoder)
                deconv_decoder = UpSampling1D(size=2, name='deconv_decoder_upsampling_{0}'.format(layer_counter))(
                    deconv_decoder)
                layer_counter += 1
        cropping_size = K.int_shape(deconv_decoder)[1] - self.output_text_size_
        if cropping_size > 0:
            deconv_decoder = Cropping1D(cropping=(0, cropping_size), name='deconv_decoder_cropping')(deconv_decoder)
        deconv_decoder = Conv1D(filters=output_vector_size, kernel_size=self.kernel_size, activation='linear',
                                padding='same', name='deconv_decoder_embeddings', trainable=True)(deconv_decoder)
        deconv_decoder_model = Model(deconv_decoder_input, deconv_decoder, name='DecoderForVAE')
        seq2seq_encoder_input = Input(shape=(self.output_text_size_, output_vector_size - 1),
                                      name='seq2seq_encoder_input')
        if self.use_attention:
            _, seq2seq_encoder_state = GRU(
                self.n_recurrent_units, return_sequences=False, return_state=True, dropout=0.5, recurrent_dropout=0.3,
                name='seq2seq_encoder_gru'
            )(Masking(mask_value=0.0, input_shape=(self.output_text_size_, output_vector_size - 1))(
                attention_for_seq2seq_encoder(seq2seq_encoder_input)
            ))
        else:
            _, seq2seq_encoder_state = GRU(
                self.n_recurrent_units, return_sequences=False, return_state=True, dropout=0.5, recurrent_dropout=0.3,
                name='seq2seq_encoder_gru'
            )(Masking(mask_value=0.0, input_shape=(self.output_text_size_, output_vector_size - 1))(
                seq2seq_encoder_input
            ))
        seq2seq_decoder_input = Input(shape=(None, len(self.target_char_index_)),
                                      name='seq2seq_decoder_input')
        seq2seq_decoder_gru = GRU(self.n_recurrent_units, return_sequences=True, return_state=True, dropout=0.5,
                                  recurrent_dropout=0.3, name='seq2seq_decoder_gru')
        seq2seq_decoder = Masking(
            mask_value=0.0, input_shape=(self.output_text_size_in_characters_, len(self.target_char_index_))
        )(seq2seq_decoder_input)
        seq2seq_decoder, _ = seq2seq_decoder_gru(seq2seq_decoder, initial_state=seq2seq_encoder_state)
        seq2seq_decoder_dense = Dense(len(self.target_char_index_), activation='softmax', name='seq2seq_decoder_dense')
        seq2seq_decoder = seq2seq_decoder_dense(seq2seq_decoder)
        vae_encoder_model = Model(encoder_input, z_mean, name='EncoderForVAE')
        generator_encoder_model = Model(seq2seq_encoder_input, seq2seq_encoder_state,
                                        name='EncoderForGenerator')
        seq2seq_state_input = Input(shape=(self.n_recurrent_units,))
        seq2seq_decoder_output, seq2seq_decoder_state = seq2seq_decoder_gru(
            seq2seq_decoder_input,
            initial_state=seq2seq_state_input
        )
        seq2seq_decoder_output = seq2seq_decoder_dense(seq2seq_decoder_output)
        generator_decoder_model = Model([seq2seq_decoder_input, seq2seq_state_input],
                                        [seq2seq_decoder_output, seq2seq_decoder_state], name='DecoderForGenerator')
        tau = 0.1
        vae_model_for_training = Model(encoder_input, deconv_decoder_model(z), name='FullVAE')
        if output_vectors is None:
            vae_model_for_training = None
            seq2seq_model_for_training = None
            weights_of_layer_for_reconstruction = None
        else:
            weights_of_layer_for_reconstruction = K.constant(output_vectors.transpose(), dtype='float32')
            seq2seq_model_for_training = Model([seq2seq_encoder_input, seq2seq_decoder_input], seq2seq_decoder,
                                               name='seq2seq_for_training')
            vae_model_for_training.compile(optimizer=Nadam(clipnorm=10.0), loss=vae_loss)
            seq2seq_model_for_training.compile(optimizer=RMSprop(lr=self.lr, clipnorm=10.0),
                                               loss='categorical_crossentropy')
            if self.verbose:
                print('')
                print('ENCODER:')
                vae_encoder_model.summary()
                print('')
                print('DECODER:')
                deconv_decoder_model.summary()
                print('')
                print('SEQUENCE-TO-SEQUENCE:')
                seq2seq_model_for_training.summary()
        return vae_encoder_model, deconv_decoder_model, generator_encoder_model, generator_decoder_model, \
               vae_model_for_training, seq2seq_model_for_training


class SequenceForVAE(Sequence):
    def __init__(self, tokenizer: BaseTokenizer, input_texts: tuple, target_texts: tuple, batch_size: int,
                 input_text_size: int, output_text_size: int, input_vocabulary: dict, input_word_vectors: np.ndarray,
                 output_vocabulary: dict, output_word_vectors: np.ndarray):
        self.tokenizer = tokenizer
        self.input_texts = input_texts
        self.target_texts = target_texts
        self.batch_size = batch_size
        self.input_text_size = input_text_size
        self.output_text_size = output_text_size
        self.n_text_pairs = len(input_texts)
        self.n_batches = self.n_text_pairs // self.batch_size
        self.input_vocabulary = input_vocabulary
        self.output_vocabulary = output_vocabulary
        self.input_word_vectors = input_word_vectors
        self.output_word_vectors = output_word_vectors

    def __len__(self):
        return self.n_batches

    def __getitem__(self, idx):
        start_pos = idx * self.batch_size
        end_pos = start_pos + self.batch_size
        input_vector_size = self.input_word_vectors.shape[1]
        output_vocabulary_size = self.output_word_vectors.shape[0]
        input_data = np.zeros((self.batch_size, self.input_text_size, input_vector_size), dtype=np.float32)
        target_data = np.zeros((self.batch_size, self.output_text_size, output_vocabulary_size), dtype=np.float32)
        idx_in_batch = 0
        for src_text_idx in range(start_pos, end_pos):
            prep_text_idx = src_text_idx
            while prep_text_idx >= self.n_text_pairs:
                prep_text_idx = prep_text_idx - self.n_text_pairs
            input_text = self.input_texts[prep_text_idx]
            prev_token = None
            for time_idx in range(self.input_text_size):
                if time_idx >= len(input_text):
                    token = ''
                else:
                    token = input_text[time_idx]
                if prev_token is None:
                    input_data[idx_in_batch, time_idx] = self.input_word_vectors[self.input_vocabulary[token]]
                else:
                    if prev_token != '':
                        input_data[idx_in_batch, time_idx] = self.input_word_vectors[self.input_vocabulary[token]]
                prev_token = token
            target_text = self.target_texts[prep_text_idx]
            prev_token = None
            for time_idx in range(self.output_text_size):
                if time_idx >= len(target_text):
                    token = ''
                else:
                    token = target_text[time_idx]
                if prev_token is None:
                    target_data[idx_in_batch, time_idx, self.output_vocabulary[token]] = 1.0
                else:
                    if prev_token != '':
                        target_data[idx_in_batch, time_idx, self.output_vocabulary[token]] = 1.0
                prev_token = token
            idx_in_batch += 1
        return input_data, target_data


class SequenceForSeq2Seq(Sequence):
    def __init__(self, tokenizer: BaseTokenizer, input_texts: tuple, target_texts: tuple, batch_size: int,
                 input_text_size: int, output_text_size: int, input_vocabulary: dict, input_word_vectors: np.ndarray,
                 output_char_index: dict, vae: Union[Model, object]):
        self.tokenizer = tokenizer
        self.input_texts = input_texts
        self.target_texts = target_texts
        self.batch_size = batch_size
        self.input_text_size = input_text_size
        self.output_text_size = output_text_size
        self.n_text_pairs = len(input_texts)
        self.n_batches = self.n_text_pairs // self.batch_size
        self.input_vocabulary = input_vocabulary
        self.input_word_vectors = input_word_vectors
        self.output_char_index = output_char_index
        self.vae = vae

    def __len__(self):
        return self.n_batches

    def __getitem__(self, idx):
        start_pos = idx * self.batch_size
        end_pos = start_pos + self.batch_size
        input_vector_size = self.input_word_vectors.shape[1]
        input_data = np.zeros((self.batch_size, self.input_text_size, input_vector_size), dtype=np.float32)
        generator_input_data = np.zeros((self.batch_size, self.output_text_size, len(self.output_char_index)),
                                        dtype=np.float32)
        generator_target_data = np.zeros((self.batch_size, self.output_text_size, len(self.output_char_index)),
                                         dtype=np.float32
                                         )
        idx_in_batch = 0
        for src_text_idx in range(start_pos, end_pos):
            prep_text_idx = src_text_idx
            while prep_text_idx >= self.n_text_pairs:
                prep_text_idx = prep_text_idx - self.n_text_pairs
            input_text = self.input_texts[prep_text_idx]
            for time_idx in range(self.input_text_size):
                if time_idx >= len(input_text):
                    token = ''
                else:
                    token = input_text[time_idx]
                input_data[idx_in_batch, time_idx] = self.input_word_vectors[self.input_vocabulary[token]]
            target_text_in_characters = self.target_texts[prep_text_idx]
            generator_input_data[idx_in_batch, 0, self.output_char_index[Conv1dTextVAE.SEQUENCE_BEGIN]] = 1.0
            T = min(len(target_text_in_characters), self.output_text_size - 3)
            for t in range(T):
                char = target_text_in_characters[t]
                generator_input_data[idx_in_batch, t + 1, self.output_char_index[char]] = 1.0
                generator_target_data[idx_in_batch, t, self.output_char_index[char]] = 1.0
            t = T
            generator_input_data[idx_in_batch, t + 1, self.output_char_index[Conv1dTextVAE.SEQUENCE_END]] = 1.0
            generator_target_data[idx_in_batch, t, self.output_char_index[Conv1dTextVAE.SEQUENCE_END]] = 1.0
            generator_target_data[idx_in_batch, t + 1, self.output_char_index[Conv1dTextVAE.SEQUENCE_END]] = 1.0
            idx_in_batch += 1
        reconstructed_word_vectors = Conv1dTextVAE.postprocess_reconstructed_word_vectors(
            self.vae.predict_on_batch(input_data)
        )
        return [reconstructed_word_vectors, generator_input_data], generator_target_data
