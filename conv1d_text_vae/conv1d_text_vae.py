import copy
import os
import math
import re
import tempfile
from typing import List, Tuple, Union

from gensim.models import FastText
import keras.backend as K
from keras.callbacks import ModelCheckpoint, EarlyStopping
from keras import Input
from keras.layers import Conv1D, Conv2DTranspose, Dense, Flatten, Reshape, Dropout, Lambda
from keras.layers import ZeroPadding1D, UpSampling1D, MaxPool1D
from keras.models import Model
from keras.optimizers import Adamax
from keras.utils import Sequence
from nltk.tokenize.nist import NISTTokenizer
import numpy as np
from scipy.spatial import distance
from sklearn.base import BaseEstimator, TransformerMixin, ClassifierMixin
from sklearn.utils.validation import check_is_fitted


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
    def __init__(self, input_embeddings: FastText, output_embeddings: FastText, tokenizer: BaseTokenizer=None,
                 n_filters: int=128, kernel_size: int=3, hidden_layer_size: int=128, latent_dim: int=50,
                 input_text_size: int=None, output_text_size: int=None, batch_size: int=64, max_epochs: int=100,
                 validation_fraction: float=0.2, warm_start: bool=False, verbose: bool=False, n_text_variants: int=3):
        self.n_filters = n_filters
        self.kernel_size = kernel_size
        self.hidden_layer_size = hidden_layer_size
        self.input_embeddings = input_embeddings
        self.output_embeddings = output_embeddings
        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self.latent_dim = latent_dim
        self.warm_start = warm_start
        self.verbose = verbose
        self.input_text_size = input_text_size
        self.output_text_size = output_text_size
        self.validation_fraction = validation_fraction
        self.tokenizer = tokenizer
        self.n_text_variants = n_text_variants

    def __del__(self):
        if hasattr(self, 'full_model_') or hasattr(self, 'encoder_model_') or hasattr(self, 'decoder_model_') or \
                hasattr(self, 'base_model_'):
            if hasattr(self, 'full_model_'):
                del self.full_model_
            if hasattr(self, 'encoder_model_'):
                del self.encoder_model_
            if hasattr(self, 'decoder_model_'):
                del self.decoder_model_
            if hasattr(self, 'base_model_'):
                del self.base_model_
            K.clear_session()
        if hasattr(self, 'input_embeddings'):
            del self.input_embeddings
        if hasattr(self, 'output_embeddings'):
            del self.output_embeddings
        if hasattr(self, 'tokenizer'):
            del self.tokenizer

    def fit(self, X: Union[list, tuple, np.ndarray], y: Union[list, tuple, np.ndarray]=None):
        self.check_params(**self.get_params(deep=False))
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
            raise ValueError(u'`validation_fraction` is too small! There are no samples for evaluation!')
        if n_eval_set >= len(X):
            raise ValueError(u'`validation_fraction` is too large! There are no samples for training!')
        if self.warm_start:
            self.check_is_fitted()
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
        if self.warm_start:
            all_weights = self.__dump_weights(self.base_model_)
            del self.full_model_, self.encoder_model_, self.decoder_model_, self.base_model_
            self.full_model_, self.encoder_model_, self.decoder_model_, self.base_model_ = \
                self.__create_model(warm_start=True)
            self.__load_weights(self.base_model_, all_weights)
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
            self.full_model_, self.encoder_model_, self.decoder_model_, self.base_model_ = self.__create_model()
        if hasattr(self.tokenizer, 'special_symbols'):
            if (self.tokenizer.special_symbols is None) or (len(self.tokenizer.special_symbols) == 0):
                special_symbols = None
            else:
                special_symbols = tuple(sorted(list(self.tokenizer.special_symbols)))
        else:
            special_symbols = None
        training_set_generator = TextPairSequence(
            input_texts=X_train, target_texts=y_train, tokenizer=self.tokenizer,
            batch_size=self.batch_size, input_text_size=self.input_text_size_, output_text_size=self.output_text_size_,
            input_embeddings=self.input_embeddings, output_embeddings=self.output_embeddings,
            special_symbols=special_symbols
        )
        evaluation_set_generator = TextPairSequence(
            input_texts=X_eval, target_texts=y_eval, tokenizer=self.tokenizer,
            batch_size=self.batch_size, input_text_size=self.input_text_size_, output_text_size=self.output_text_size_,
            input_embeddings=self.input_embeddings, output_embeddings=self.output_embeddings,
            special_symbols=special_symbols
        )
        callbacks = [
            EarlyStopping(patience=5, verbose=(1 if self.verbose else 0))
        ]
        tmp_weights_name = self.get_temp_name()
        try:
            callbacks.append(
                ModelCheckpoint(filepath=tmp_weights_name, verbose=(1 if self.verbose else 0), save_best_only=True,
                                save_weights_only=True)
            )
            self.full_model_.fit_generator(
                generator=training_set_generator,
                epochs=self.max_epochs, verbose=False,
                shuffle=True,
                validation_data=evaluation_set_generator,
                callbacks=callbacks
            )
            if os.path.isfile(tmp_weights_name):
                self.full_model_.load_weights(tmp_weights_name)
        finally:
            if os.path.isfile(tmp_weights_name):
                os.remove(tmp_weights_name)
        if self.warm_start:
            name_of_embedding_layer = 'encoder_embeddings'
            for layer in self.full_model_.layers:
                if layer.name != name_of_embedding_layer:
                    layer.trainable = True
        return self

    def transform(self, X: Union[list, tuple, np.ndarray]) -> np.ndarray:
        self.check_is_fitted()
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
            outputs_for_batch = self.encoder_model_.predict(data_for_batch)
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
        self.check_texts_param(X, 'X')
        generated_texts = []
        if self.tokenizer is None:
            self.tokenizer = DefaultTokenizer()
        start_pos = 0
        if hasattr(self.tokenizer, 'special_symbols'):
            if self.tokenizer.special_symbols is not None:
                special_symbols = tuple(sorted(list(self.tokenizer.special_symbols)))
            else:
                special_symbols = None
        else:
            special_symbols = None
        for data_for_batch in self.texts_to_data(X, self.batch_size, self.input_text_size_, self.tokenizer,
                                                 self.input_embeddings, special_symbols):
            outputs_for_batch = self.full_model_.predict(data_for_batch)
            end_pos = start_pos + outputs_for_batch.shape[0]
            if (end_pos + start_pos) > len(X):
                end_pos = len(X)
            n_texts_in_batch = end_pos - start_pos
            for sample_idx in range(n_texts_in_batch):
                words_of_text = []
                for time_idx in range(outputs_for_batch.shape[1]):
                    best_variants_of_word = self.find_best_words(outputs_for_batch[sample_idx][time_idx],
                                                                 self.output_embeddings, self.n_text_variants,
                                                                 special_symbols)
                    if best_variants_of_word is None:
                        break
                    if len(best_variants_of_word) > 0:
                        words_of_text.append(tuple(best_variants_of_word))
                if len(words_of_text) > 0:
                    generated_texts.append(tuple(self.find_best_texts(words_of_text, self.n_text_variants)))
                else:
                    generated_texts.append(tuple([]))
        return (np.array(generated_texts, dtype=object) if isinstance(X, np.ndarray) else (
            tuple(generated_texts) if isinstance(X, tuple) else generated_texts))

    def fit_transform(self, X, y=None, **fit_params):
        return self.fit(X, y).transform(X)

    def fit_predict(self, X, y=None, **fit_params):
        return self.fit(X, y).predict(X)

    def get_params(self, deep=True):
        return {
            'n_filters': self.n_filters,
            'kernel_size': self.kernel_size,
            'hidden_layer_size': self.hidden_layer_size,
            'input_embeddings': (Conv1dTextVAE.copy_embeddings(self.input_embeddings) if deep
                                 else self.input_embeddings),
            'output_embeddings': (Conv1dTextVAE.copy_embeddings(self.output_embeddings) if deep
                                  else self.output_embeddings),
            'batch_size': self.batch_size,
            'max_epochs': self.max_epochs,
            'latent_dim': self.latent_dim,
            'warm_start': self.warm_start,
            'verbose': self.verbose,
            'input_text_size': self.input_text_size,
            'output_text_size': self.output_text_size,
            'validation_fraction': self.validation_fraction,
            'n_text_variants': self.n_text_variants,
            'tokenizer': None if self.tokenizer is None else (copy.deepcopy(self.tokenizer) if deep else self.tokenizer),
        }

    def set_params(self, **params):
        self.n_filters = params['n_filters']
        self.kernel_size = params['kernel_size']
        self.hidden_layer_size = params['hidden_layer_size']
        self.input_embeddings = params['input_embeddings']
        self.output_embeddings = params['output_embeddings']
        self.batch_size = params['batch_size']
        self.max_epochs = params['max_epochs']
        self.latent_dim = params['latent_dim']
        self.warm_start = params['warm_start']
        self.verbose = params['verbose']
        self.input_text_size = params['input_text_size']
        self.output_text_size = params['output_text_size']
        self.validation_fraction = params['validation_fraction']
        self.tokenizer = params['tokenizer']
        self.n_text_variants = params['n_text_variants']

    def check_is_fitted(self):
        check_is_fitted(self, ['input_text_size_', 'output_text_size_', 'full_model_', 'encoder_model_',
                               'decoder_model_', 'base_model_'])

    @staticmethod
    def find_best_words(word_vector: np.ndarray, embeddings_model: FastText, n: int,
                        special_symbols: Tuple[str]=None) -> Union[List[tuple], None]:
        vector_size = embeddings_model.vector_size + 2
        if special_symbols is not None:
            vector_size += len(special_symbols)
        norm_value = np.linalg.norm(word_vector[:embeddings_model.vector_size])
        if norm_value < K.epsilon():
            norm_value = 1.0
        res = embeddings_model.wv.similar_by_vector(word_vector[:embeddings_model.vector_size] / norm_value, topn=n)
        best_vector = np.zeros((vector_size,), dtype=np.float32)
        best_vector[0:embeddings_model.vector_size] = embeddings_model.wv[res[0][0]]
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
        return res

    @staticmethod
    def find_best_texts(variants_of_text: List[tuple], ntop: int) -> List[str]:
        used_variants = []
        variants_and_similarities = []
        new_variant = []
        for word_idx in range(len(variants_of_text)):
            variants_of_word = variants_of_text[word_idx]
            new_variant.append(variants_of_word[0][0])
            for variant_idx in range(1, len(variants_of_word)):
                variants_and_similarities.append(((word_idx, variant_idx), variants_of_word[variant_idx][1]))
        used_variants.append(' '.join(new_variant))
        variants_and_similarities.sort(key=lambda it: (-it[1], it[0][0], it[0][1]))
        for variant_idx in range(min(ntop - 1, len(variants_and_similarities))):
            word_idx = variants_and_similarities[variant_idx][0][0]
            variants_of_word = variants_of_text[word_idx]
            best_variant_idx = variants_and_similarities[variant_idx][0][1]
            new_variant[word_idx] = variants_of_word[best_variant_idx][0]
            used_variants.append(' '.join(new_variant))
        return used_variants

    @staticmethod
    def copy_embeddings(src: FastText) -> FastText:
        tmp_fasttext_name = Conv1dTextVAE.get_temp_name()
        try:
            src.save(tmp_fasttext_name)
            src.wv.save(tmp_fasttext_name + '.wv')
            res = FastText.load(tmp_fasttext_name)
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
        if 'input_embeddings' not in params:
            raise ValueError('The parameter `input_embeddings` is not defined!')
        if not isinstance(params['input_embeddings'], FastText):
            raise ValueError('The parameter `input_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(FastText()), type(params['input_embeddings'])))
        if 'output_embeddings' not in params:
            raise ValueError('The parameter `output_embeddings` is not defined!')
        if not isinstance(params['output_embeddings'], FastText):
            raise ValueError('The parameter `output_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(FastText()), type(params['output_embeddings'])))
        if 'warm_start' not in params:
            raise ValueError('The parameter `warm_start` is not defined!')
        if (not isinstance(params['warm_start'], bool)) and (not isinstance(params['warm_start'], int)):
            raise ValueError('The parameter `warm_start` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(True), type(params['warm_start'])))
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
        if 'n_filters' not in params:
            raise ValueError('The parameter `n_filters` is not defined!')
        if not isinstance(params['n_filters'], int):
            raise ValueError('The parameter `n_filters` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['n_filters'])))
        if params['n_filters'] <= 0:
            raise ValueError('The parameter `n_filters` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['n_filters']))
        if 'kernel_size' not in params:
            raise ValueError('The parameter `kernel_size` is not defined!')
        if not isinstance(params['kernel_size'], int):
            raise ValueError('The parameter `kernel_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['kernel_size'])))
        if params['kernel_size'] <= 0:
            raise ValueError('The parameter `kernel_size` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['kernel_size']))
        if 'hidden_layer_size' not in params:
            raise ValueError('The parameter `hidden_layer_size` is not defined!')
        if not isinstance(params['hidden_layer_size'], int):
            raise ValueError('The parameter `hidden_layer_size` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['hidden_layer_size'])))
        if params['hidden_layer_size'] <= 0:
            raise ValueError('The parameter `hidden_layer_size` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['hidden_layer_size']))
        if 'validation_fraction' not in params:
            raise ValueError('The parameter `validation_fraction` is not defined!')
        if not isinstance(params['validation_fraction'], float):
            raise ValueError('The parameter `validation_fraction` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10.5), type(params['validation_fraction'])))
        if (params['validation_fraction'] <= 0) or (params['validation_fraction'] >= 1.0):
            raise ValueError('The parameter `validation_fraction` is wrong! Expected a positive value between 0.0 and '
                             '1.0, but {0} does not correspond to this condition.'.format(
                Conv1dTextVAE.float_to_string(params['validation_fraction'])))
        if 'n_text_variants' not in params:
            raise ValueError('The parameter `n_text_variants` is not defined!')
        if not isinstance(params['n_text_variants'], int):
            raise ValueError('The parameter `n_text_variants` is wrong! Expected `{0}`, got `{1}`.'.format(
                type(10), type(params['n_text_variants'])))
        if params['n_text_variants'] <= 0:
            raise ValueError('The parameter `n_text_variants` is wrong! Expected a positive value, '
                             'but {0} is not positive.'.format(params['n_text_variants']))

    @staticmethod
    def tokenize(src: str, bounds_of_words: List[Tuple[int, int]]) -> tuple:
        return tuple(
            filter(
                lambda it2: len(it2) > 0, map(lambda it1: src[it1[0]:it1[1]].strip().lower(), bounds_of_words)
            )
        )

    @staticmethod
    def texts_to_data(input_texts: Union[list, tuple, np.ndarray], batch_size: int, max_text_size: int,
                      tokenizer: BaseTokenizer, fasttext_model: FastText, special_symbols: tuple=None):
        n_batches = int(math.ceil(len(input_texts) / batch_size))
        vector_size = fasttext_model.vector_size + 2
        if (special_symbols is not None) and (len(special_symbols) > 0):
            vector_size += len(special_symbols)
        for batch_ind in range(n_batches):
            input_data = np.zeros((batch_size, max_text_size, vector_size), dtype=np.float32)
            start_pos = batch_ind * batch_size
            end_pos = start_pos + batch_size
            for src_text_idx in range(start_pos, end_pos):
                for time_idx in range(max_text_size):
                    input_data[src_text_idx - start_pos, time_idx, vector_size - 1] = 1.0
            for src_text_idx in range(start_pos, end_pos):
                prep_text_idx = src_text_idx
                if src_text_idx >= len(input_texts):
                    prep_text_idx = len(input_texts) - 1
                input_text = input_texts[prep_text_idx]
                bounds_of_input_words = tokenizer.tokenize_into_words(input_text)
                for time_idx, token in enumerate(Conv1dTextVAE.tokenize(input_text, bounds_of_input_words)):
                    if time_idx >= max_text_size:
                        break
                    if (special_symbols is not None) and (token in special_symbols):
                        input_data[src_text_idx - start_pos, time_idx,
                                   fasttext_model.vector_size + special_symbols.index(token)] = 1.0
                    else:
                        try:
                            word_vector = fasttext_model.wv[token]
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
                    input_data[src_text_idx - start_pos, time_idx, vector_size - 1] = 0.0
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

    def __load_fasttext_model(self, data_as_bytes: dict) -> FastText:
        if not isinstance(data_as_bytes, dict):
            raise ValueError(u'The `data_as_bytes` must be a `{0}`, not `{1}`!'.format(
                type({1: 'a', 2: 'b'}), type(data_as_bytes)))
        for cur_key in data_as_bytes:
            if (not isinstance(data_as_bytes[cur_key], bytearray)) and (not isinstance(data_as_bytes[cur_key], bytes)):
                raise ValueError(u'The `data_as_bytes[{0}]` must be an array of bytes, not `{1}`!'.format(
                    cur_key, type(data_as_bytes)))
            if not cur_key.startswith('model'):
                raise ValueError('The `{0}` is bad name for the fasttext data. '
                                 'All names must be start with `model`.'.format(cur_key))
            if cur_key != 'model':
                if not cur_key.endswith('.npy'):
                    raise ValueError('The `{0}` is bad name for the fasttext data. '
                                     'All names must be end with `.npy`.'.format(cur_key))
        tmp_model_name = self.get_temp_name()
        try:
            with open(tmp_model_name, 'wb') as fp:
                fp.write(data_as_bytes['model'])
            for cur_key in data_as_bytes.keys():
                if cur_key == 'model':
                    continue
                additional_name = tmp_model_name + cur_key[len('model'):]
                with open(additional_name, 'wb') as fp:
                    fp.write(data_as_bytes[cur_key])
            model = FastText.load(tmp_model_name)
        finally:
            self.remove_fasttext_files(tmp_model_name)
        return model

    def __dump_fasttext_model(self, model: FastText) -> dict:
        tmp_model_name = self.get_temp_name()
        weights_of_model = dict()
        try:
            self.remove_fasttext_files(tmp_model_name)
            model.save(tmp_model_name)
            model.wv.save(tmp_model_name + '.wv')
            with open(tmp_model_name, 'rb') as fp:
                weights_of_model['model'] = fp.read()
            dir_name = os.path.dirname(tmp_model_name)
            base_name = os.path.basename(tmp_model_name)
            for additional_name in filter(lambda it: it.startswith(base_name) and it.endswith('.npy'),
                                          os.listdir(dir_name)):
                with open(os.path.join(dir_name, additional_name), 'rb') as fp:
                    weights_of_model['model' + additional_name[len(base_name):]] = fp.read()
        finally:
            self.remove_fasttext_files(tmp_model_name)
        return weights_of_model

    def __load_weights(self, model: Model, weights_as_bytes: Union[bytearray, bytes]):
        if (not isinstance(weights_as_bytes, bytearray)) and (not isinstance(weights_as_bytes, bytes)):
            raise ValueError(u'The `weights_as_bytes` must be an array of bytes, not `{0}`!'.format(
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
        state['input_embeddings'] = self.__dump_fasttext_model(self.input_embeddings)
        state['output_embeddings'] = (None if (self.input_embeddings is self.output_embeddings) else
                                      self.__dump_fasttext_model(self.output_embeddings))
        if all(map(lambda it: hasattr(self, it),
                   ['input_text_size_', 'output_text_size_', 'full_model_', 'encoder_model_', 'decoder_model_'])):
            state['input_text_size_'] = self.input_text_size_
            state['output_text_size_'] = self.output_text_size_
            state['weights_'] = self.__dump_weights(self.full_model_)
        return state

    def __setstate__(self, state):
        if not isinstance(state, dict):
            raise ValueError(u'`state` is wrong! Expected {0}.'.format(type({0: 1})))
        if 'input_embeddings' not in state:
            raise ValueError('The parameter `input_embeddings` is not defined!')
        if not isinstance(state['input_embeddings'], dict):
            raise ValueError('The parameter `input_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                type({1: 'a', 2: 'b'}), type(state['input_embeddings'])))
        if 'output_embeddings' not in state:
            raise ValueError('The parameter `output_embeddings` is not defined!')
        if (not isinstance(state['output_embeddings'], dict)) and (state['output_embeddings'] is not None):
            raise ValueError('The parameter `output_embeddings` is wrong! Expected `{0}`, got `{1}`.'.format(
                type({'a': 1, 'b': 2}), type(state['output_embeddings'])))
        state['input_embeddings'] = self.__load_fasttext_model(state['input_embeddings'])
        state['output_embeddings'] = (state['input_embeddings'] if state['output_embeddings'] is None
                                      else self.__load_fasttext_model(state['output_embeddings']))
        self.check_params(**state)
        if hasattr(self, 'full_model_') or hasattr(self, 'encoder_model_') or hasattr(self, 'decoder_model_') or \
                hasattr(self, 'base_model_'):
            if hasattr(self, 'full_model_'):
                del self.full_model_
            if hasattr(self, 'encoder_model_'):
                del self.encoder_model_
            if hasattr(self, 'decoder_model_'):
                del self.decoder_model_
            if hasattr(self, 'base_model_'):
                del self.base_model_
            K.clear_session()
        is_fitted = all(map(lambda it: it in state, ['input_text_size_', 'output_text_size_', 'weights_']))
        self.set_params(**state)
        if is_fitted:
            self.input_text_size_ = state['input_text_size_']
            self.output_text_size_ = state['output_text_size_']
            self.full_model_, self.encoder_model_, self.decoder_model_, self.base_model_ = self.__create_model()
            self.__load_weights(self.full_model_, state['weights_'])

    def __create_model(self, warm_start: bool=False) -> Tuple[Model, Model, Model, Model]:

        def sampling(args):
            z_mean_, z_log_var_ = args
            epsilon = K.random_normal(shape=(K.shape(z_mean_)[0], self.latent_dim), mean=0.0, stddev=1.0)
            return z_mean_ + K.exp(z_log_var_) * epsilon

        def normalize_outputs(x):
            return K.l2_normalize(x, axis=-1)

        def vae_loss(y_true, y_pred):
            cosine_loss = K.mean(1 - K.sum((y_true * y_pred), axis=-1))
            kl_loss = -5e-4 * K.mean(1 + z_log_var - K.square(z_mean) - K.exp(z_log_var), axis=-1)
            return K.mean(cosine_loss + kl_loss)

        def Conv1DTranspose(input_tensor, filters, kernel_size, strides=1, padding='same', activation='relu',
                            name: str="", trainable: bool=True):
            x = Lambda(lambda x: K.expand_dims(x, axis=2), name=name+'_deconv1d_part1')(input_tensor)
            x = Conv2DTranspose(filters=filters, kernel_size=(kernel_size, 1), activation=activation,
                                strides=(strides, 1), padding=padding, name=name+'_deconv1d_part2',
                                trainable=trainable)(x)
            x = Lambda(lambda x: K.squeeze(x, axis=2), name='deconv1d_part3')(x)
            return x

        encoder_input = Input(shape=(self.input_text_size_, self.input_embeddings.vector_size + 2), dtype='float32',
                              name='encoder_embeddings')
        encoder = Conv1D(filters=self.n_filters, kernel_size=self.kernel_size, activation='relu',
                         padding='same', name='encoder_conv1d', trainable=(not warm_start))(encoder_input)
        shape_before_flattening = K.int_shape(encoder)
        encoder = Dense(self.hidden_layer_size, activation='relu', name='encoder_dense', trainable=(not warm_start))(
            Dropout(0.5, name='encoder_dropout')(Flatten(name='encoder_flatten')(encoder)))
        z_mean = Dense(self.latent_dim, name='z_mean', trainable=(not warm_start))(encoder)
        z_log_var = Dense(self.latent_dim, name='z_log_var', trainable=(not warm_start))(encoder)
        z = Lambda(sampling, name='z')([z_mean, z_log_var])
        decoder_input = Input(K.int_shape(z)[1:], name='decoder_input')
        decoder = Dense(np.prod(shape_before_flattening[1:]), activation='relu', name='decoder_dense',
                        trainable=True)(Dropout(0.5, name='decoder_dropout')(decoder_input))
        decoder = Reshape(shape_before_flattening[1:], name='decoder_reshape')(decoder)
        decoder = Conv1DTranspose(decoder, filters=self.n_filters, kernel_size=self.kernel_size, activation='relu',
                                  name='decoder', trainable=True)
        base_decoder_model = Model(decoder_input, decoder, name='BaseDecoderModel')
        if self.input_text_size_ != self.output_text_size_:
            if self.input_text_size_ < self.output_text_size_:
                k = int(math.floor(self.output_text_size_ / self.input_text_size_))
                if k > 1:
                    decoder = UpSampling1D(size=k, name='decoder_upsampling')(decoder)
                padding = self.output_text_size_ - self.input_text_size_ * k
                if padding > 0:
                    decoder = ZeroPadding1D(padding=(0, padding), name='decoder_padding')(decoder)
            else:
                k = int(math.ceil(self.input_text_size_ / self.output_text_size_))
                if (self.output_text_size_ * k) > self.input_text_size_:
                    padding = (self.output_text_size_ * k) - self.input_text_size_
                    decoder = ZeroPadding1D(padding=(0, padding), name='decoder_padding')(decoder)
                decoder = MaxPool1D(pool_size=k, name='decoder_pooling')(decoder)
        decoder = Conv1D(filters=self.output_embeddings.vector_size + 2, kernel_size=self.kernel_size,
                         activation='linear', padding='same', name='decoder_embeddings', trainable=True)(decoder)
        decoder = Lambda(normalize_outputs, name='decoder_normalize')(decoder)
        encoder_model = Model(encoder_input, z, name='EncoderModel')
        decoder_model = Model(decoder_input, decoder, name='DecoderModel')
        base_model = Model(encoder_input, base_decoder_model(z), name='BaseVAE')
        full_model = Model(encoder_input, decoder_model(z), name='FullVAE')
        full_model.compile(optimizer=Adamax(clipnorm=10.0), loss=vae_loss)
        if self.verbose:
            print('')
            print('ENCODER:')
            encoder_model.summary(positions=[0.34, 0.6, 0.72, 1.0], line_length=80)
            print('')
            print('DECODER:')
            decoder_model.summary(positions=[0.34, 0.6, 0.72, 1.0], line_length=80)
        return full_model, encoder_model, decoder_model, base_model


class TextPairSequence(Sequence):
    def __init__(self, tokenizer, input_texts, target_texts, batch_size, input_text_size, output_text_size,
                 input_embeddings, output_embeddings, special_symbols):
        self.tokenizer = tokenizer
        self.input_texts = list()
        self.target_texts = list()
        self.batch_size = batch_size
        self.input_text_size = input_text_size
        self.output_text_size = output_text_size
        self.n_text_pairs = len(input_texts)
        self.n_batches = self.n_text_pairs // self.batch_size
        self.special_symbols = special_symbols
        self.input_vocabulary = dict()
        for idx in range(len(input_texts)):
            cur_text = input_texts[idx]
            bounds_of_words = self.tokenizer.tokenize_into_words(cur_text)
            words = Conv1dTextVAE.tokenize(cur_text, bounds_of_words)
            self.input_texts.append(words)
            for cur_word in words:
                if cur_word not in self.input_vocabulary:
                    try:
                        word_vector = input_embeddings.wv[cur_word]
                    except:
                        word_vector = None
                    if word_vector is not None:
                        vector_norm = np.linalg.norm(word_vector)
                        if vector_norm < K.epsilon():
                            vector_norm = 1.0
                        self.input_vocabulary[cur_word] = word_vector / vector_norm
        self.output_vocabulary = dict()
        for idx in range(len(target_texts)):
            cur_text = target_texts[idx]
            bounds_of_words = self.tokenizer.tokenize_into_words(cur_text)
            words = Conv1dTextVAE.tokenize(cur_text, bounds_of_words)
            self.target_texts.append(words)
            for cur_word in words:
                if cur_word not in self.output_vocabulary:
                    try:
                        word_vector = output_embeddings.wv[cur_word]
                    except:
                        word_vector = None
                    if word_vector is not None:
                        vector_norm = np.linalg.norm(word_vector)
                        if vector_norm < K.epsilon():
                            vector_norm = 1.0
                        self.output_vocabulary[cur_word] = word_vector / vector_norm
        self.input_vector_size = input_embeddings.vector_size
        self.output_vector_size = output_embeddings.vector_size

    def __len__(self):
        return self.n_batches

    def __getitem__(self, idx):
        start_pos = idx * self.batch_size
        end_pos = start_pos + self.batch_size
        input_vector_size = self.input_vector_size + 2
        output_vector_size = self.output_vector_size + 2
        if (self.special_symbols is not None) and (len(self.special_symbols) > 0):
            input_vector_size += len(self.special_symbols)
            output_vector_size += len(self.special_symbols)
        input_data = np.zeros((self.batch_size, self.input_text_size, input_vector_size), dtype=np.float32)
        target_data = np.zeros((self.batch_size, self.output_text_size, output_vector_size), dtype=np.float32)
        for idx_in_batch in range(self.batch_size):
            for time_idx in range(self.input_text_size):
                input_data[idx_in_batch, time_idx, input_vector_size - 1] = 1.0
            for time_idx in range(self.output_text_size):
                target_data[idx_in_batch, time_idx, output_vector_size - 1] = 1.0
        idx_in_batch = 0
        for src_text_idx in range(start_pos, end_pos):
            prep_text_idx = src_text_idx
            while prep_text_idx >= self.n_text_pairs:
                prep_text_idx = prep_text_idx - self.n_text_pairs
            input_text = self.input_texts[prep_text_idx]
            for time_idx, token in enumerate(input_text):
                if time_idx >= self.input_text_size:
                    break
                if (self.special_symbols is not None) and (token in self.special_symbols):
                    input_data[idx_in_batch, time_idx, self.input_vector_size + self.special_symbols.index(token)] = 1.0
                else:
                    word_vector = self.input_vocabulary.get(token, None)
                    if word_vector is not None:
                        input_data[idx_in_batch, time_idx, 0:self.input_vector_size] = word_vector
                    else:
                        input_data[idx_in_batch, time_idx, input_vector_size - 2] = 1.0
                input_data[idx_in_batch, time_idx, input_vector_size - 1] = 0.0
            target_text = self.target_texts[prep_text_idx]
            for time_idx, token in enumerate(target_text):
                if time_idx >= self.output_text_size:
                    break
                if (self.special_symbols is not None) and (token in self.special_symbols):
                    target_data[idx_in_batch, time_idx, self.output_vector_size +
                                self.special_symbols.index(token)] = 1.0
                else:
                    word_vector = self.output_vocabulary.get(token, None)
                    if word_vector is None:
                        target_data[idx_in_batch, time_idx, output_vector_size - 2] = 1.0
                    else:
                        target_data[idx_in_batch, time_idx, 0:self.output_vector_size] = word_vector
                target_data[idx_in_batch, time_idx, output_vector_size - 1] = 0.0
            idx_in_batch += 1
        return input_data, target_data