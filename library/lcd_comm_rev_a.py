import struct
import time

from serial.tools.list_ports import comports

from library.lcd_comm import *
from library.log import logger


class Command(IntEnum):
    RESET = 101  # Resets the display
    CLEAR = 102  # Clears the display to a white screen
    TO_BLACK = 103  # Makes the screen go black. NOT TESTED
    SCREEN_OFF = 108  # Turns the screen off
    SCREEN_ON = 109  # Turns the screen on
    SET_BRIGHTNESS = 110  # Sets the screen brightness
    SET_ORIENTATION = 121  # Sets the screen orientation
    DISPLAY_BITMAP = 197  # Displays an image on the screen


class LcdCommRevA(LcdComm):
    def __init__(self, com_port: str = "AUTO", display_width: int = 320, display_height: int = 480):
        super().__init__(com_port, display_width, display_height)
        self.openSerial()

    def __del__(self):
        self.closeSerial()

    @staticmethod
    def auto_detect_com_port():
        com_ports = serial.tools.list_ports.comports()
        auto_com_port = None

        for com_port in com_ports:
            if com_port.serial_number == "USB35INCHIPSV2":
                auto_com_port = com_port.device

        return auto_com_port

    def SendCommand(self, cmd: Command, x: int, y: int, ex: int, ey: int):

        # Commands must be sent at least 'inter_bitmap_delay' after the bitmap data.
        delay = (self.last_bitmap_time + self.inter_bitmap_delay) - time.time()
        if delay > 0:
            time.sleep(delay)

        byteBuffer = bytearray(6)
        byteBuffer[0] = (x >> 2)
        byteBuffer[1] = (((x & 3) << 6) + (y >> 4))
        byteBuffer[2] = (((y & 15) << 4) + (ex >> 6))
        byteBuffer[3] = (((ex & 63) << 2) + (ey >> 8))
        byteBuffer[4] = (ey & 255)
        byteBuffer[5] = cmd

        # If no queue for async requests, or if asked explicitly to do the request sequentially: do request now
        self.WriteData(byteBuffer)

    def InitializeComm(self):
        # HW revision A does not need init commands
        pass

    def Reset(self):
        logger.info("Display reset (COM port may change)...")

        with self.com_mutex:
            self.SendCommand(Command.RESET, 0, 0, 0, 0)

            # NOTE: Shouldn't we close serial before we try to open it again ?

            # Wait for display reset then reconnect
            time.sleep(1)
            self.openSerial()

    def Clear(self):
        self.SetOrientation(Orientation.PORTRAIT)  # Bug: orientation needs to be PORTRAIT before clearing
        with self.com_mutex:
            self.SendCommand(Command.CLEAR, 0, 0, 0, 0)
        self.SetOrientation()  # Restore default orientation

    def ScreenOff(self):
        with self.com_mutex:
            self.SendCommand(Command.SCREEN_OFF, 0, 0, 0, 0)

    def ScreenOn(self):
        with self.com_mutex:
            self.SendCommand(Command.SCREEN_ON, 0, 0, 0, 0)

    def SetBrightness(self, level: int = 25):
        assert 0 <= level <= 100, 'Brightness level must be [0-100]'

        # Display scales from 0 to 255, with 0 being the brightest and 255 being the darkest.
        # Convert our brightness % to an absolute value.
        level_absolute = int(255 - ((level / 100) * 255))

        # Level : 0 (brightest) - 255 (darkest)
        with self.com_mutex:
            self.SendCommand(Command.SET_BRIGHTNESS, level_absolute, 0, 0, 0)

    def SetBackplateLedColor(self, led_color: tuple[int, int, int] = (255, 255, 255)):
        logger.info("HW revision A does not support backplate LED color setting")
        pass

    def SetOrientation(self, orientation: Orientation = Orientation.PORTRAIT):
        self.orientation = orientation
        width = self.get_width()
        height = self.get_height()
        x = 0
        y = 0
        ex = 0
        ey = 0
        byteBuffer = bytearray(11)
        byteBuffer[0] = (x >> 2)
        byteBuffer[1] = (((x & 3) << 6) + (y >> 4))
        byteBuffer[2] = (((y & 15) << 4) + (ex >> 6))
        byteBuffer[3] = (((ex & 63) << 2) + (ey >> 8))
        byteBuffer[4] = (ey & 255)
        byteBuffer[5] = Command.SET_ORIENTATION
        byteBuffer[6] = (orientation + 100)
        byteBuffer[7] = (width >> 8)
        byteBuffer[8] = (width & 255)
        byteBuffer[9] = (height >> 8)
        byteBuffer[10] = (height & 255)
        with self.com_mutex:
            self.lcd_serial.write(bytes(byteBuffer))

    def DisplayPILImage(
            self,
            image: Image,
            x: int = 0, y: int = 0,
            image_width: int = 0,
            image_height: int = 0
    ):
        # If the image height/width isn't provided, use the native image size
        if not image_height:
            image_height = image.size[1]
        if not image_width:
            image_width = image.size[0]

        # If our image is bigger than our display, resize it to fit our screen
        if image.size[1] > self.get_height():
            image_height = self.get_height()
        if image.size[0] > self.get_width():
            image_width = self.get_width()

        assert x <= self.get_width(), 'Image X coordinate must be <= display width'
        assert y <= self.get_height(), 'Image Y coordinate must be <= display height'
        assert image_height > 0, 'Image width must be > 0'
        assert image_width > 0, 'Image height must be > 0'

        (x0, y0) = (x, y)
        (x1, y1) = (x + image_width - 1, y + image_height - 1)

        with self.com_mutex:
            self.SendCommand(Command.DISPLAY_BITMAP, x0, y0, x1, y1)

            pix = image.load()
            line = bytes()

            for h in range(image_height):
                for w in range(image_width):
                    R = pix[w, h][0] >> 3
                    G = pix[w, h][1] >> 2
                    B = pix[w, h][2] >> 3

                    rgb = (R << 11) | (G << 5) | B
                    line += struct.pack('H', rgb)

                    # Send image data by multiple of DISPLAY_WIDTH bytes
                    if len(line) >= self.get_width() * 8:
                        self.WriteLine(line)
                        line = bytes()

            # Write last line if needed
            if len(line) > 0:
                self.WriteLine(line)

            # There must be a short period between the last write of the bitmap data and the next
            # command. This seems to be around 0.02s on the flagship device.
            self.last_bitmap_time = time.time()
