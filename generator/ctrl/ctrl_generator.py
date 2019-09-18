import tensorflow as tf
import numpy as np

tf.enable_eager_execution()
import generator.ctrl.model.transformer as transformer
import re
from collections import Counter
from tensorflow.python import debug as tf_debug
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import embedding_ops
import generator.ctrl.model.fastBPE as fastBPE

pos_action_starts = ["You attack", "You tell", "You use", "You go"]

# the loss function is a simple categorical crossentropy between the logits and the labels
def loss(labels, logits):
    return tf.keras.losses.spae_categorical_crossentropy(labels, logits, from_logits=True)


class CTRLGenerator():

    def __init__(self):

        generate_num=256
        model_dir = "generator/ctrl/model/seqlen256_v1.ckpt/"

        # load the vocabulary from file
        vocab = open('generator/ctrl/model/vocab', encoding='utf-8').read().split('\n')
        vocab = list(map(lambda x: x.split(' ')[0], vocab)) + ['<unk>'] + ['\n']
        print('{} unique words'.format(len(vocab)))

        # length of the vocabulary
        vocab_size = len(vocab)

        # define the numericalization map
        # idx2word maps the numericalized ID to the word
        # word2idx maps the word to the numericalized ID
        self.word2idx = {u: i for i, u in enumerate(vocab)}
        self.idx2word = np.array(vocab)

        # sequence length to use for the transformer
        # the model is trained with a self.seq_length of 512
        # so, any value <= 512 should work
        self.seq_length = min(generate_num, 256)

        # the dimension of the transformer
        embedding_dim = 1280

        # input for the keras model
        tokens = tf.keras.layers.Input(shape=(self.seq_length,), dtype='int32')

        # Now, we begin defining the model
        # we defer the transformer definition to transformer.py
        # here, we only define the tied softmax layer
        # this layer ties the softmax weights to the input embeddings
        class TiedEmbeddingSoftmax(tf.keras.layers.Layer):

            def __init__(self, vocab_size=vocab_size, embedding_size=embedding_dim, **kwargs):
                super(TiedEmbeddingSoftmax, self).__init__()
                self.w = self.add_weight(name='w', shape=(vocab_size, embedding_size),
                                         initializer='random_normal',
                                         trainable=True)
                self.b = self.add_weight(name='b', shape=(vocab_size,),
                                         initializer='zeros',
                                         trainable=True)

            def call(self, inputs, embed=True):
                if embed:
                    dtype = tf.keras.backend.dtype(inputs)
                    if dtype != 'int32' and dtype != 'int64':
                        inputs = math_ops.cast(inputs, 'int32')
                    return embedding_ops.embedding_lookup(self.w, inputs)
                else:
                    return tf.tensordot(inputs, tf.transpose(self.w), 1) + self.b

        # instantiates a tied softmax class
        tied_embedding_softmax = TiedEmbeddingSoftmax()

        # embedded tokens, before passing it to the transformer
        embedded = tied_embedding_softmax(tokens, embed=True)

        # the activations after passing it from the transformer
        # for some odd reason, TPUs don't play well with specifying the arguments of the Encoder() function
        # so you have to leave them at their defaults
        transformed = transformer.Encoder()(embedded, training=False)

        # pass the activations from our tiedsoftmax class
        # this time with embed=False denoting that we are doing the softmax operation
        # and not a lookup
        logits = tied_embedding_softmax(transformed, embed=False)

        # finally, define the Keras model with inputs as tokens and outputs as the logits we just computed
        model = tf.keras.Model(inputs=tokens, outputs=logits)

        # the optimizer is not used since this code only supports inference
        # however, to compile the model, we still define it
        optimizer = tf.contrib.tpu.CrossShardOptimizer(
            tf.contrib.estimator.clip_gradients_by_norm(
                tf.train.AdagradOptimizer(learning_rate=1e-2), 0.25)
        )

        # compile the model with the optimizer and loss
        model.compile(optimizer=optimizer, loss=loss)
        print(model.summary())

        # IMPORTANT
        # this is where the saved model is presented to the code
        # the model directory should have the model checkpoint and
        # a checkpoint file
        run_config = tf.contrib.tpu.RunConfig(
            model_dir=model_dir)

        # this converts the Keras model to a TensorFlow estimator
        # this step is critical
        # remember to patch the TF 1.14 file before running the code, else you're going to see errors here
        estimator_model = tf.keras.estimator.model_to_estimator(keras_model=model, config=run_config)

        # we now create a serving function from this estimator
        # this enables us to load the model once and easily query it multiple times
        def serving_input_fn():
            inputs = {'input_1': tf.placeholder(tf.int32, [1, self.seq_length])}
            return tf.estimator.export.ServingInputReceiver(inputs, inputs)

        self.predict_fn = tf.contrib.predictor.from_estimator(estimator_model, serving_input_fn)

        # almost there, we now take the user prompt and tokenize with BPE
        # load BPE codes
        self.bpe = fastBPE.fastBPE('codes', 'vocab')

        self.temperature = 0
        self.nucleusprob = 0
        self.penalty = 1.2
        self.topk = 0


    def generate(self, prompt):

        # tokenize provided prompt
        split_prompt = self.bpe.apply([prompt])[0].split()
        text = [self.word2idx[i] for i in split_prompt]

        # pad with 0s and create a mini-batch of 2 (arbitrary, for ease of code)
        padded_text = text + [0] * (self.generate_num - len(text))
        tokens_generated = np.tile(padded_text, (1, 1))
        result = ""
        try:
            for token in range(len(text) - 1, self.generate_num - 1):
                # get the logits from the prediction function
                # the logic here is a bit convoluted because we are allowing generation past 512 tokens
                # this is done by sliding the window over (past 512 tokens) and continuing prediction
                # I'm sure this can be simplified (TODO)
                if token <= self.seq_length:
                    prompt_logits = self.predict_fn({'input_1': tokens_generated[:, :self.seq_length]})[
                                        'tied_embedding_softmax'].squeeze() / (self.temperature if self.temperature > 0 else 1.)
                    _token = token if token < self.seq_length else -1
                else:
                    _token = -1
                    end = token + 1
                    start = token - self.seq_length + 2
                    prompt_logits = \
                        self.predict_fn({'input_1': np.hstack((tokens_generated[:, 0:1], tokens_generated[:, start:end]))})[
                            'tied_embedding_softmax'].squeeze() / (self.temperature if self.temperature > 0 else 1.)

                # if penalty (for repetition) is non-zero,
                # discount the logits from already generated tokens
                if self.penalty > 0:
                    penalized_so_far = set()
                    for _ in range(token + 1):
                        generated_token = tokens_generated[0][_]
                        # don't penalize newlines
                        # you could also choose not to penalize frequent words
                        # (which incidentally are sorted in the vocab file)
                        # but I don't do that
                        # if it prints too many new lines instead of continuing generating text,
                        # you might want to comment this out
                        if self.idx2word[generated_token] == '\n':
                            continue
                        if generated_token in penalized_so_far:
                            continue
                        penalized_so_far.add(generated_token)
                        prompt_logits[_token][generated_token] /= self.penalty

                # disallow some tokens
                prompt_logits[_token][self.word2idx['<unk>']] = -1e8

                # sometimes, when generating from reddit,
                # it tries to generate the Score (reddit Karma) immediately after generating the Title:
                # to disallow this, we can just prevent it from generating Score
                prompt_logits[_token][self.word2idx['Sco@@']] = -1e8

                # compute probabilities from logits
                prompt_probs = np.exp(prompt_logits[_token])
                prompt_probs = prompt_probs / sum(prompt_probs)
                pruned_list = np.argsort(prompt_probs)[::-1]
                # if you are using nucleus prob, then compute the nucleus probability size
                if self.nucleusprob > 0.:
                    minimum_topk = 1
                    nucleus = max(np.where(np.cumsum(np.sort(prompt_probs)[::-1]) > self.nucleusprob)[0][0], minimum_topk)
                elif self.topk > 0:
                    # we are over-loading notation here
                    # if you choose to specify a topk instead of a nucleus,
                    # we will hardcode the nucleus to be just that
                    nucleus = self.topk
                else:
                    # if you specify neither nucleus or topk,
                    # then we will use the whole list
                    nucleus = len(pruned_list)

                # if you want to disallow more complex tokens, you can do so here
                # for instance, if you want to disallow anything with the phrase `http`,
                # you can delete theme from the pruned_list
                # you can comment this out, I'm keeping it in for demonstration purpose
                tokens_to_disallow = []
                for _ in range(len(pruned_list)):
                    if 'http' in self.idx2word[pruned_list[_]]:
                        tokens_to_disallow.append(_)
                pruned_list = np.delete(pruned_list, tokens_to_disallow)

                # if temperature is 0
                # just pick the first (most probable) token
                if self.temperature == 0:
                    idx = pruned_list[0]
                else:
                    # else,
                    # sample from the pruned_list with the logits
                    chosen_idx = int(tf.random.categorical(np.expand_dims(prompt_logits[0][_token][pruned_list], 0),
                                                           num_samples=1).numpy())
                    idx = pruned_list[chosen_idx]

                # if you want to do some debugging,
                # like which one was chosen,
                # what the top25 were,
                # here is your opportunity.
                # print('chosen:', idx2word[idx])
                # print('top25 alternatives:', pruned_list[:25])

                # assign the token for generation
                tokens_generated[0][token + 1] = idx

                # clear screen if you want to
                # os.system("clear")

                tokens_generated_so_far = ' '.join([self.idx2word[c] for c in tokens_generated[0].squeeze()[:token + 2]])
                tokens_generated_so_far = re.sub('(@@ )', '', string=tokens_generated_so_far)
                tokens_generated_so_far = re.sub('(@@ ?$)', '', string=tokens_generated_so_far)

                result = tokens_generated_so_far

        except:
            print("Error in generation")

        return result