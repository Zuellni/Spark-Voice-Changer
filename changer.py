from argparse import ArgumentParser
from queue import Queue
from typing import Literal

import numpy as np
import sounddevice as sd

from models import Codec, Spark, Whisper
from utils import Logger, Timer


class Changer:
    @staticmethod
    def devices(kind: Literal["input", "output"]) -> tuple[list[int], str]:
        device_list = []
        device_ids = []

        for device in sd.query_devices():
            if device[f"max_{kind}_channels"] and device["hostapi"] == 0:
                device_list.append(f"{device['index']}: {device['name']}")
                device_ids.append(device["index"])

        return device_ids, ", ".join(device_list)

    def __init__(
        self,
        input: int,
        output: int,
        audio: str,
        codec: str,
        spark: str,
        whisper: str,
        sample_rate: int = 16000,
        block_duration: int = 30,
        detection_threshold: float = 0.01,
        silence_threshold: int = 5,
        queue_threshold: int = 10,
        sleep: int = 100,
    ) -> None:
        self.input = input
        self.output = output
        self.audio = audio
        self.codec = codec
        self.spark = spark
        self.whisper = whisper

        self.sample_rate = sample_rate
        self.block_size = int(sample_rate * block_duration / 1000)
        self.detection_threshold = detection_threshold
        self.silence_threshold = silence_threshold
        self.queue_threshold = queue_threshold
        self.sleep = sleep

        self.silence_counter = 0
        self.queue = Queue()

    def callback(
        self, data: np.ndarray, frames: int, _, status: sd.CallbackFlags
    ) -> None:
        if frames != self.block_size or status:
            Logger.error("Recording error")
            return

        if np.sqrt(np.mean(data**2)) >= self.detection_threshold:
            self.queue.put(data.copy())
            self.silence_counter = 0

    def __call__(self) -> None:
        with Timer("Loaded codec"):
            codec = Codec(self.codec)

        with Timer("Loaded spark"):
            spark = Spark(self.spark)

        with Timer("Loaded whisper"):
            whisper = Whisper(self.whisper)

        with Timer("Encoded audio"):
            audio, audio_tokens = codec.encode(self.audio)
            codec.warmup(audio)

        Logger.info("Listening")

        with sd.InputStream(
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            device=self.input,
            channels=1,
            callback=self.callback,
        ):
            try:
                while True:
                    sd.sleep(self.sleep)

                    if self.silence_counter < self.silence_threshold:
                        self.silence_counter += 1
                        continue

                    if self.queue.qsize() < self.queue_threshold:
                        continue

                    self.silence_counter = 0

                    with Timer() as timer:
                        try:
                            data = [self.queue.get() for _ in range(self.queue.qsize())]
                            data = np.concatenate(data).squeeze()
                            text = whisper(data)
                        except:
                            Logger.error("Transcription error")
                            continue

                    if text:
                        timer(f'Transcribed "{text}"')
                    else:
                        Logger.warn("Empty transcript")
                        continue

                    with Timer() as timer:
                        try:
                            tokens = spark(text, audio_tokens)
                        except:
                            Logger.error("Generation error")
                            continue

                        try:
                            data = codec.decode(audio, tokens)
                        except:
                            Logger.error("Decoding error")
                            continue

                    timer(
                        f"Generated {data.shape[0] / self.sample_rate:.2f} "
                        "seconds of audio"
                    )

                    try:
                        sd.play(
                            data=data,
                            samplerate=self.sample_rate,
                            blocking=True,
                            device=self.output,
                        )
                    except:
                        Logger.error("Playback error")
            except KeyboardInterrupt:
                Logger.warn("Quitting")
                spark.unload()


if __name__ == "__main__":
    ic, ih = Changer.devices("input")
    oc, oh = Changer.devices("output")

    parser = ArgumentParser()
    parser.add_argument("-i", "--input", type=int, choices=ic, required=True, help=ih)
    parser.add_argument("-o", "--output", type=int, choices=oc, required=True, help=oh)
    parser.add_argument("-a", "--audio", required=True, help="audio file")
    parser.add_argument("-c", "--codec", default="sparkaudio/spark-tts-0.5b")
    parser.add_argument("-s", "--spark", required=True, help="gguf model")
    parser.add_argument("-w", "--whisper", default="turbo")
    args = parser.parse_args()

    changer = Changer(
        input=args.input,
        output=args.output,
        audio=args.audio,
        codec=args.codec,
        spark=args.spark,
        whisper=args.whisper,
    )

    changer()
