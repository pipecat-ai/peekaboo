#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pipecat.bus.messages import BusFrameMessage, BusMessage
from pipecat.bus.subscriber import BusSubscriber
from pipecat.frames.frames import Frame, UserImageRawFrame, UserImageRequestFrame
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)

# Bridge name the screen worker accepts frames from (``bridged=(SCREEN_BRIDGE,)``).
SCREEN_BRIDGE = "voice"

SCREEN_VIDEO_SOURCE = "screenVideo"


def screen_frame_request(client_id: str = "") -> UserImageRequestFrame:
    """A request for one frame of the shared screen.

    Pushed upstream towards the transport, which serves it with the next
    screen frame. The image is never appended to the voice LLM context; the
    vision worker is the only consumer.
    """
    return UserImageRequestFrame(
        user_id=client_id,
        video_source=SCREEN_VIDEO_SOURCE,
        append_to_context=False,
    )


class ScreenBridge(FrameProcessor, BusSubscriber):
    """Moves screen frames between the transport pipeline and the vision worker.

    Sits right after the transport input. Screen images the transport captures
    go onto the bus, tagged with the bridge name the screen worker accepts, and
    stop here: nothing else in the voice pipeline wants them. Image requests
    the screen worker pushes upstream come back over the bus and continue
    upstream to the transport, which serves them. Daily needs the client id on
    a request (WebRTC ignores it), so the bridge fills it in.
    """

    def __init__(self, *, screen_worker_name: str, **kwargs):
        super().__init__(**kwargs)
        self._screen_worker_name = screen_worker_name
        self._client_id = ""

    def set_client_id(self, client_id: str):
        self._client_id = client_id

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        await self.pipeline_worker.bus.subscribe(self)

    async def cleanup(self):
        await super().cleanup()
        await self.pipeline_worker.bus.unsubscribe(self)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserImageRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.pipeline_worker.bus.send(
                BusFrameMessage(
                    source=self.pipeline_worker.name,
                    frame=frame,
                    direction=direction,
                    bridge=SCREEN_BRIDGE,
                )
            )
            return

        await self.push_frame(frame, direction)

    async def on_bus_message(self, message: BusMessage) -> None:
        if not isinstance(message, BusFrameMessage):
            return
        if message.source != self._screen_worker_name:
            return
        if not isinstance(message.frame, UserImageRequestFrame):
            return

        frame = message.frame
        if not frame.user_id:
            frame.user_id = self._client_id

        await self.push_frame(frame, FrameDirection.UPSTREAM)
