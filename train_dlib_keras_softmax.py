import os
import pathlib

import cv2
import numpy as np
import tensorflow as tf
from tensorflow.keras.models import Model
from sklearn import preprocessing
from tensorflow_core.python.keras.layers import Input, Dense, Flatten, BatchNormalization, Activation

import utils
from dlib_tool.converter.model import build_dlib_model
from dlib_tool.converter.weights import load_weights

tf.random.set_seed(9075)
SIZE = 224
IMG_SHAPE = (SIZE, SIZE, 3)
SHAPE = (SIZE, SIZE)
BATCH_SIZE = 64
AUTOTUNE = tf.data.experimental.AUTOTUNE
CLASS_NAMES = np.array([])
EPOCHS = 10
TRAIN_DATA_PATH = 'images/public_face_1036_224_train/'
# TRAIN_DATA_PATH = 'images/glint_2w/'
VER_RECORD = 'verification.tfrecord'
OUTPUT_MODEL_LOGS_FOLDER = 'model_out/keras_logs'
OUTPUT_MODEL_FOLDER_CKPT = 'model_out/keras_ckpt'
OUTPUT_EMB_MODEL_FOLDER = 'model_out/keras_embedding'
PATIENCE = 100
EMB_SIZE = 128
IS_CENTER_LOSS = True
CENTER_LOSS_LAMBDA = 0.01


class DummyLayer(tf.keras.layers.Layer):
    def __init__(self, **kwargs):
        super(DummyLayer, self).__init__(**kwargs)

    def call(self, inputs, **kwargs):
        return inputs


class L2EmbeddingLayer(tf.keras.layers.Layer):
    def __init__(self):
        super(L2EmbeddingLayer, self).__init__()

    def call(self, inputs, **kwargs):
        return tf.nn.l2_normalize(inputs, axis=1)


class L2WeightLayer(tf.keras.layers.Layer):
    def __init__(self, num_outputs, **kwargs):
        super(L2WeightLayer, self).__init__(**kwargs)
        self.num_outputs = num_outputs
        self.kernel = None

    def build(self, input_shape):
        self.kernel = self.add_variable("kernel",
                                        shape=[int(input_shape[-1]),
                                               self.num_outputs])

    def call(self, inputs, **kwargs):
        kernel = tf.nn.l2_normalize(self.kernel, axis=0)
        result = tf.matmul(inputs, kernel)
        return tf.nn.softmax(result)


class CenterLossLayer(tf.keras.layers.Layer):
    def __init__(self, nrof_classes, alpha=0.5, **kwargs):
        super().__init__(**kwargs)
        self.alpha = alpha
        self.centers = None
        self.nrof_classes = nrof_classes
        self.result = None

    def build(self, input_shape):
        self.centers = self.add_weight(name='centers',
                                       shape=(self.nrof_classes, input_shape[0][-1]),
                                       initializer='uniform',
                                       trainable=False)

    def call(self, inputs, mask=None):
        # inputs[0] = embedding, inputs[1] = gt_labels
        # update first
        delta_centers = tf.matmul(tf.transpose(inputs[1]), (tf.matmul(inputs[1], self.centers) - inputs[0]))
        center_counts = tf.math.reduce_sum(tf.transpose(inputs[1]), axis=1, keepdims=True) + 1
        delta_centers /= center_counts
        new_centers = self.centers - self.alpha * delta_centers
        self.add_update((self.centers, new_centers), inputs)

        # then calculate loss
        result = inputs[0] - tf.matmul(inputs[1], self.centers)
        self.result = tf.math.reduce_sum(result ** 2, axis=1, keepdims=True)
        return self.result  # Nx1

    def compute_output_shape(self, input_shape):
        return tf.shape(self.result)


def get_label(file_path):
    parts = tf.strings.split(file_path, '/')
    one_hot = tf.cast(parts[-2] == CLASS_NAMES, tf.float32)
    return tf.argmax(one_hot), one_hot


def decode_img(img):
    img = tf.image.decode_jpeg(img, channels=3)
    img = tf.image.random_brightness(img, 0.2)
    img = tf.image.random_saturation(img, 0.6, 1.6)
    img = tf.image.random_contrast(img, 0.6, 1.4)
    img = tf.image.random_flip_left_right(img)
    img = tf.image.convert_image_dtype(img, tf.float32)
    img = tf.subtract(img, 0.5)
    img = tf.multiply(img, 2.0)
    return img


def process_path(file_path):
    label, one_hot = get_label(file_path)
    img = tf.io.read_file(file_path)
    img = decode_img(img)
    return (img, one_hot), (label, tf.constant(0))


def process_onehot(file_path):
    parts = tf.strings.split(file_path, '/')
    one_hot = tf.cast(parts[-2] == CLASS_NAMES, tf.int8)
    return one_hot, tf.constant(0)  # second is dummy


def prepare_for_training(ds, cache=False, shuffle_buffer_size=2000):
    if cache:
        if isinstance(cache, str):
            ds = ds.cache(cache)
        else:
            ds = ds.cache()

    ds = ds.shuffle(buffer_size=shuffle_buffer_size)
    ds = ds.repeat()
    ds = ds.batch(BATCH_SIZE)
    ds = ds.prefetch(buffer_size=AUTOTUNE)

    return ds


def zero_loss(y_true, y_pred):  # y_true is dummy
    return 0.5 * tf.reduce_sum(y_pred, axis=0)


def main():
    global CLASS_NAMES

    train_data_dir = pathlib.Path(TRAIN_DATA_PATH)
    train_list_ds = tf.data.Dataset.list_files(str(train_data_dir / '*/*.jpg'))
    CLASS_NAMES = np.array([item.name for item in train_data_dir.glob('*') if item.name not in [".keep", ".DS_Store"]])
    train_image_count = len(list(train_data_dir.glob('*/*.jpg')))
    steps_per_epoch = np.ceil(train_image_count / BATCH_SIZE)

    print('total labels: %d' % len(CLASS_NAMES))

    train_main_ds = train_list_ds.map(process_path, num_parallel_calls=AUTOTUNE)
    train_main_ds = prepare_for_training(train_main_ds)

    main_input = Input(IMG_SHAPE)
    side_input = Input(len(CLASS_NAMES))

    x = DummyLayer(name='dummy_input')(main_input)
    x = build_dlib_model(x, use_bn=True)
    x = Flatten()(x)
    x = Dense(EMB_SIZE, name='embedding', use_bias=False)(x)
    x = Activation(tf.nn.relu6)(x)
    main_loss = Dense(len(CLASS_NAMES), name='main', use_bias=False, activation=tf.nn.softmax)(x)
    side_loss = CenterLossLayer(len(CLASS_NAMES), name='center')([x, side_input])

    model = Model(inputs=[main_input, side_input], outputs=[main_loss, side_loss])
    load_weights(model, 'dlib_tool/dlib_face_recognition_resnet_model_v1.xml', use_bn=True)

    model.compile(optimizer=tf.keras.optimizers.Adam(),
                  loss=['sparse_categorical_crossentropy', zero_loss],
                  loss_weights=[1, CENTER_LOSS_LAMBDA],
                  metrics={'main': 'sparse_categorical_accuracy'})
    model.summary()

    save_cb = tf.keras.callbacks.ModelCheckpoint(OUTPUT_MODEL_FOLDER_CKPT, monitor='val_loss', verbose=1,
                                                 save_weights_only=False, mode='auto')
    summary_cb = tf.keras.callbacks.TensorBoard(OUTPUT_MODEL_LOGS_FOLDER, histogram_freq=1)

    iter = train_main_ds.make_one_shot_iterator()
    next = iter.get_next()
    # result = model.predict([next[0][0], next[0][1], next[1][0], next[1][1])

    write_line('start:')
    debug_layers = ['dummy_input', 'max_pooling2d', 'activation_2', 'activation_4', 'activation_6', 'activation_8',
                    'activation_10', 'activation_12', 'activation_14', 'activation_16', 'activation_18',
                    'activation_20', 'activation_22', 'activation_24', 'activation_26', 'activation_28', 'embedding']
    for layer in debug_layers:
        debug_model = tf.keras.models.Model(inputs=model.input[0], outputs=model.get_layer(name=layer).output)
        model_record(debug_model, prefix=layer)

    model.fit(train_main_ds,
              epochs=EPOCHS,
              steps_per_epoch=steps_per_epoch,
              callbacks=[OutputCallback()])
    # callbacks=[save_cb, SaveBestValCallback(), summary_cb])


class SaveBestValCallback(tf.keras.callbacks.Callback):

    def __init__(self):
        super().__init__()
        self.embedding_model = None
        self.best_acc = 0

    def set_model(self, model):
        self.model = model
        embedding = model.get_layer(name='embedding')
        self.embedding_model = tf.keras.models.Model(inputs=model.input[0], outputs=embedding.output)

    def on_epoch_end(self, epoch, logs=None):
        verification_path = os.path.join('tfrecord', VER_RECORD)
        ver_dataset = utils.get_ver_data(verification_path, SHAPE)

        first_list, second_list, true_same = ver_dataset[0], ver_dataset[1], np.array(ver_dataset[2])
        total = len(true_same)

        dist_list = []
        for idx, (img1, img2) in enumerate(zip(first_list, second_list)):
            h, w, _ = img1.shape
            result1 = self.embedding_model.predict(np.expand_dims(img1, axis=0))
            result2 = self.embedding_model.predict(np.expand_dims(img2, axis=0))
            result1 = preprocessing.normalize(result1, norm='l2')
            result2 = preprocessing.normalize(result2, norm='l2')

            dist = np.linalg.norm(result1 - result2)
            dist_list.append(dist)

        thresholds = np.arange(0.1, 3.0, 0.05)

        accs = []
        for threshold in thresholds:
            pred_same = np.less(dist_list, threshold)
            tp = np.sum(np.logical_and(pred_same, true_same))
            tn = np.sum(np.logical_and(np.logical_not(pred_same), np.logical_not(true_same)))
            acc = float(tp + tn) / total
            accs.append(acc)
        best_index = int(np.argmax(accs))

        val_acc = accs[best_index]
        val_thr = thresholds[best_index]

        print('\n val_acc: %f, val_thr: %f, current best: %f ' % (val_acc, val_thr, self.best_acc))
        if self.best_acc < val_acc:
            self.embedding_model.save(OUTPUT_EMB_MODEL_FOLDER)
            self.best_acc = val_acc


def model_record(model, prefix=''):
    img = cv2.imread('images/0001_01.png')
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = cv2.resize(img, SHAPE)
    img = img - 127.5
    img = img * 0.0078125

    result = model.predict(np.expand_dims(img, axis=0))[0]
    result = result.flatten()

    write_line(f'{prefix}:\n{result[:10]}')


def write_line(result):
    with open("record.txt", "a") as myfile:
        myfile.writelines(f'{result}\n')


class OutputCallback(tf.keras.callbacks.Callback):

    def __init__(self):
        super().__init__()

    def on_epoch_end(self, epoch, logs=None):
        write_line(f'epoch:{epoch}:')
        debug_layers = ['max_pooling2d', 'activation_2', 'activation_4', 'activation_6', 'activation_8',
                        'activation_10', 'activation_12', 'activation_14', 'activation_16', 'activation_18',
                        'activation_20', 'activation_22', 'activation_24', 'activation_26', 'activation_28',
                        'embedding']
        for layer in debug_layers:
            debug_model = tf.keras.models.Model(inputs=self.model.input[0],
                                                outputs=self.model.get_layer(name=layer).output)
            model_record(debug_model, prefix=layer)


if __name__ == '__main__':
    main()