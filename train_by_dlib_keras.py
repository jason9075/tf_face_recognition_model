import os
import pathlib

import numpy as np
import tensorflow as tf
from sklearn import preprocessing

import utils
from dlib_tool.converter.model import build_dlib_model
from dlib_tool.converter.weights import load_weights

tf.random.set_seed(9075)
IMG_SHAPE = (224, 224, 3)
SHAPE = (224, 224)
BATCH_SIZE = 64
AUTOTUNE = tf.data.experimental.AUTOTUNE
CLASS_NAMES = np.array([])
EPOCHS = 30000
TRAIN_DATA_PATH = 'images/public_face_1036_224_train/'
# TRAIN_DATA_PATH = 'images/star224/'
TEST_DATA_PATH = 'images/public_face_1036_224_valid/'
# TEST_DATA_PATH = 'images/star224/'
OUTPUT_MODEL_LOGS_FOLDER = 'model_out/keras_logs'
OUTPUT_MODEL_FOLDER_CKPT = 'model_out/keras_ckpt'
OUTPUT_EMB_MODEL_FOLDER = 'model_out/keras_embedding'
PATIENCE = 100
EMB_SIZE = 128


class DummyLayer(tf.keras.layers.Layer):
    def __init__(self):
        super(DummyLayer, self).__init__()

    def call(self, inputs, **kwargs):
        return inputs


class L2EmbeddingLayer(tf.keras.layers.Layer):
    def __init__(self):
        super(L2EmbeddingLayer, self).__init__()

    def call(self, inputs, **kwargs):
        return tf.nn.l2_normalize(inputs, axis=1)


class L2WeightLayer(tf.keras.layers.Layer):
    def __init__(self, num_outputs):
        super(L2WeightLayer, self).__init__()
        self.num_outputs = num_outputs
        self.kernel = None

    def build(self, input_shape):
        self.kernel = self.add_variable("kernel",
                                        shape=[int(input_shape[-1]),
                                               self.num_outputs])

    def call(self, inputs, **kwargs):
        kernel = tf.nn.l2_normalize(self.kernel, axis=0)
        kernel = tf.matmul(inputs, kernel)
        return tf.nn.softmax(kernel)


def get_label(file_path):
    parts = tf.strings.split(file_path, '/')
    one_hot = tf.cast(parts[-2] == CLASS_NAMES, tf.int8)
    return tf.argmax(one_hot)


def decode_img(img):
    img = tf.image.decode_jpeg(img, channels=3)  # value from 0 ~ 1
    img = tf.image.convert_image_dtype(img, tf.float32)
    img = tf.image.resize(img, [IMG_SHAPE[0], IMG_SHAPE[1]])
    img = tf.image.random_brightness(img, 0.2)
    img = tf.image.random_saturation(img, 0.6, 1.6)
    img = tf.image.random_contrast(img, 0.6, 1.4)
    img = tf.image.random_flip_left_right(img)
    img = tf.subtract(img, 0.5)
    img = tf.multiply(img, 2)
    return img


def process_path(file_path):
    label = get_label(file_path)
    img = tf.io.read_file(file_path)
    img = decode_img(img)
    return img, label


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


def main():
    global CLASS_NAMES

    train_data_dir = pathlib.Path(TRAIN_DATA_PATH)
    list_ds = tf.data.Dataset.list_files(str(train_data_dir / '*/*.jpg'))
    CLASS_NAMES = np.array([item.name for item in train_data_dir.glob('*') if item.name not in [".keep", ".DS_Store"]])
    test_data_dir = pathlib.Path(TEST_DATA_PATH)
    test_list_ds = tf.data.Dataset.list_files(str(test_data_dir / '*/*.jpg'))
    train_image_count = len(list(train_data_dir.glob('*/*.jpg')))
    test_image_count = len(list(test_data_dir.glob('*/*.jpg')))
    steps_per_epoch = np.ceil(train_image_count / BATCH_SIZE)
    val_steps = np.ceil(test_image_count / BATCH_SIZE)

    print('total labels: %d' % len(CLASS_NAMES))

    train_labeled_ds = list_ds.map(process_path, num_parallel_calls=AUTOTUNE)
    train_ds = prepare_for_training(train_labeled_ds)
    test_labeled_ds = test_list_ds.map(process_path, num_parallel_calls=AUTOTUNE)

    keras_model = build_dlib_model(image_h=SHAPE[0], image_w=SHAPE[1], use_bn=True)
    load_weights(keras_model, 'dlib_tool/dlib_face_recognition_resnet_model_v1.xml')

    model = tf.keras.Sequential([
        keras_model,
        L2EmbeddingLayer(),
        L2WeightLayer(len(CLASS_NAMES)),
    ])

    model.summary()

    model.compile(optimizer=tf.keras.optimizers.Adam(),
                  loss='sparse_categorical_crossentropy',
                  metrics=['sparse_categorical_accuracy'])

    save_cb = tf.keras.callbacks.ModelCheckpoint(OUTPUT_MODEL_FOLDER_CKPT, monitor='val_loss', verbose=1,
                                                 save_weights_only=False, mode='auto')
    summary_cb = tf.keras.callbacks.TensorBoard(OUTPUT_MODEL_LOGS_FOLDER, histogram_freq=1)

    model.fit(train_ds,
              epochs=EPOCHS,
              steps_per_epoch=steps_per_epoch,
              validation_data=test_labeled_ds.batch(BATCH_SIZE),
              validation_steps=val_steps,
              callbacks=[SaveBestValCallback()])
    # callbacks=[save_cb, SaveBestValCallback(), summary_cb])

    loss, accuracy = model.evaluate(test_labeled_ds.batch(BATCH_SIZE), verbose=2)
    print("Loss :", loss)
    print("Accuracy :", accuracy)


class SaveBestValCallback(tf.keras.callbacks.Callback):

    def __init__(self):
        super().__init__()
        self.embedding_model = None
        self.best_acc = 0

    def set_model(self, model):
        self.model = model
        embedding = model.layers[1].output
        self.embedding_model = tf.keras.models.Model(inputs=model.input, outputs=embedding)

    def on_epoch_end(self, epoch, logs=None):
        verification_path = os.path.join('tfrecord', 'verification.tfrecord')
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


if __name__ == '__main__':
    main()
