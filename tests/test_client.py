import io
import os
import pty
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from zalman_lcd.client import LcdClient
from zalman_lcd.device import Display, DeviceError, _rle
from zalman_lcd.media import encode_jpeg
import numpy as np


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock()
        self.factory = patch('zalman_lcd.client.Display', return_value=self.device).start()
        self.find = patch('zalman_lcd.client.find_tty', return_value='/dev/ttyACM4').start()
        self.addCleanup(patch.stopall)
        self.client = LcdClient('usb1', 6)

    def test_dashboard_updates_do_not_reflash_or_reopen(self):
        self.client.set_color('FF0000')
        self.client.set_color('00FF00')
        self.client.set_color('00FF00')
        self.factory.assert_called_once_with('/dev/ttyACM4')
        self.find.assert_called_once_with('usb1', 6)
        self.device.video_download.assert_called_once_with(1, 1)
        self.assertEqual(self.device.send_overlay.call_count, 2)
        self.assertEqual(self.device.send_jpeg.call_count, 1)
        self.assertTrue(np.all(self.device.send_overlay.call_args.args[0] == 0xff00ff00))

    def test_write_failure_closes_and_next_update_reconnects(self):
        self.device.send_overlay.side_effect = [DeviceError('timeout'), None]
        with self.assertRaises(DeviceError):
            self.client.set_color('red')
        self.device.close.assert_called_once()
        self.client.set_color('red')
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.device.video_download.call_count, 2)

    def test_missing_matching_port_never_opens_fallback(self):
        self.find.return_value = None
        with self.assertRaises(DeviceError):
            self.client.set_color('blue')
        self.factory.assert_not_called()

    def test_invalid_settings_do_not_open_hardware(self):
        for value in (-1, 101):
            with self.assertRaises(ValueError):
                self.client.set_brightness(value)
        with self.assertRaises(ValueError):
            self.client.set_orientation(45)
        self.factory.assert_not_called()

    def test_orientation_redraws_existing_image(self):
        im = Image.new('RGBA', (320, 320), 'black')
        im.putpixel((0, 0), (255, 0, 0, 255))
        self.client._image = im
        self.client.set_orientation(90)
        pixels = self.device.send_overlay.call_args.args[0].reshape(320, 320)
        self.assertEqual(pixels[319, 0], 0xffff0000)


class ProtocolTests(unittest.TestCase):
    def test_clean_jpeg_strips_comment_and_exif(self):
        im = Image.new('RGB', (320, 320), 'red')
        im.info['comment'] = b'dangerous metadata'
        im.info['exif'] = b'Exif\x00\x00metadata'
        jpeg = encode_jpeg(im)
        with Image.open(io.BytesIO(jpeg)) as result:
            self.assertEqual(result.size, (320, 320))
            self.assertNotIn('comment', result.info)
            self.assertNotIn('exif', result.info)
            self.assertFalse(result.info.get('progressive'))
        self.assertLessEqual(len(jpeg), 14000)

    def test_rle_solid_and_literal_wire_format(self):
        import struct
        self.assertEqual(_rle(np.full(5, 0xff00ff00, dtype=np.uint32)),
                         struct.pack('<II', 0x02000005, 0xff00ff00))
        self.assertEqual(_rle(np.array([1, 2, 3], dtype=np.uint32)),
                         struct.pack('<IIII', 0x01000003, 1, 2, 3))

    def test_second_serial_owner_rejected_before_protocol(self):
        master, slave = pty.openpty()
        path = os.ttyname(slave)
        try:
            with patch('zalman_lcd.device.fcntl.ioctl'), patch.object(Display, 'wake') as wake:
                first = Display(path)
                try:
                    with self.assertRaisesRegex(DeviceError, 'already in use'):
                        Display(path)
                    self.assertEqual(wake.call_count, 1)
                finally:
                    first.close()
        finally:
            os.close(slave)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
