'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2017  Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

import os, errno
# import sys, platform, getpass
import csv_utils
from pyglui import ui
import numpy as np
# from scipy.interpolate import UnivariateSpline
from plugin import Plugin
from time import strftime, localtime, time, gmtime
from shutil import copy2
from audio import Audio_Input_Dict
from file_methods import save_object, load_object
from methods import get_system_info
from av_writer import JPEG_Writer, AV_Writer, Audio_Capture
from ndsi import H264Writer
from calibration_routines.camera_intrinsics_estimation import load_camera_calibration
# logging
import logging
logger = logging.getLogger(__name__)


def get_auto_name():
    return strftime("%Y_%m_%d", localtime())

# def sanitize_timestamps(ts):
#     logger.debug("Checking %s timestamps for monotony in direction and smoothness"%ts.shape[0])
#     avg_frame_time = (ts[-1] - ts[0])/ts.shape[0]
#     logger.debug('average_frame_time: %s'%(1./avg_frame_time))

#     raw_ts = ts #only needed for visualization
#     runs = 0
#     while True:
#         #forward check for non monotonic increasing behaviour
#         clean = np.ones((ts.shape[0]),dtype=np.bool)
#         damper  = 0
#         for idx in range(ts.shape[0]-1):
#             if ts[idx] >= ts[idx+1]: #not monotonically increasing timestamp
#                 damper = 50
#             clean[idx] = damper <= 0
#             damper -=1

#         #backward check to smooth timejumps forward
#         damper  = 0
#         for idx in range(ts.shape[0]-1)[::-1]:
#             if ts[idx+1]-ts[idx]>1: #more than one second forward jump
#                 damper = 50
#             clean[idx] &= damper <= 0
#             damper -=1

#         if clean.all() == True:
#             if runs >0:
#                 logger.debug("Timestamps were bad but are ok now. Correction runs: %s"%runs)
#                 # from matplotlib import pyplot as plt
#                 # plt.plot(frames,raw_ts)
#                 # plt.plot(frames,ts)
#                 # # plt.scatter(frames[~clean],ts[~clean])
#                 # plt.show()
#             else:
#                 logger.debug("Timestamps are clean.")
#             return ts

#         runs +=1
#         if runs > 4:
#             logger.error("Timestamps could not be fixed!")
#             return ts

#         logger.warning("Timestamps are not sane. We detected non monotitc or jumpy timestamps. Fixing them now")
#         frames = np.arange(len(ts))
#         s = UnivariateSpline(frames[clean],ts[clean],s=0)
#         ts = s(frames)


class Recorder(Plugin):
    """Capture Recorder"""
    def __init__(self, g_pool, session_name=get_auto_name(), rec_dir=None,
                 user_info={'name': '', 'additional_field': 'change_me'},
                 info_menu_conf={}, show_info_menu=False, record_eye=False,
                 audio_src='No Audio', raw_jpeg=True):
        super().__init__(g_pool)
        # update name if it was autogenerated.
        if session_name.startswith('20') and len(session_name) == 10:
            session_name = get_auto_name()

        base_dir = self.g_pool.user_dir.rsplit(os.path.sep, 1)[0]
        default_rec_dir = os.path.join(base_dir, 'recordings')

        if rec_dir and rec_dir != default_rec_dir and self.verify_path(rec_dir):
            self.rec_dir = rec_dir
        else:
            try:
                os.makedirs(default_rec_dir)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    logger.error("Could not create Rec dir")
                    raise e
            else:
                logger.info('Created standard Rec dir at "{}"'.format(default_rec_dir))
            self.rec_dir = default_rec_dir

        self.raw_jpeg = raw_jpeg
        self.order = .9
        self.record_eye = record_eye
        self.session_name = session_name
        self.audio_devices_dict = Audio_Input_Dict()
        if audio_src in list(self.audio_devices_dict.keys()):
            self.audio_src = audio_src
        else:
            self.audio_src = 'No Audio'
        self.running = False
        self.menu = None
        self.button = None

        self.user_info = user_info
        self.show_info_menu = show_info_menu
        self.info_menu = None
        self.info_menu_conf = info_menu_conf

    def get_init_dict(self):
        d = {}
        d['record_eye'] = self.record_eye
        d['audio_src'] = self.audio_src
        d['session_name'] = self.session_name
        d['user_info'] = self.user_info
        d['info_menu_conf'] = self.info_menu_conf
        d['show_info_menu'] = self.show_info_menu
        d['rec_dir'] = self.rec_dir
        d['raw_jpeg'] = self.raw_jpeg
        return d

    def init_gui(self):
        self.menu = ui.Growing_Menu('Recorder')
        self.menu.collapsed = True
        self.g_pool.sidebar.insert(3, self.menu)
        self.menu.append(ui.Info_Text('Pupil recordings are saved like this: "path_to_recordings/recording_session_name/nnn" where "nnn" is an increasing number to avoid overwrites. You can use "/" in your session name to create subdirectories.'))
        self.menu.append(ui.Info_Text('Recordings are saved to "~/pupil_recordings". You can change the path here but note that invalid input will be ignored.'))
        self.menu.append(ui.Text_Input('rec_dir', self, setter=self.set_rec_dir, label='Path to recordings'))
        self.menu.append(ui.Text_Input('session_name', self, setter=self.set_session_name, label='Recording session name'))
        self.menu.append(ui.Switch('show_info_menu', self, on_val=True, off_val=False, label='Request additional user info'))
        self.menu.append(ui.Selector('raw_jpeg', self, selection=[True, False], labels=["bigger file, less CPU", "smaller file, more CPU"], label='Compression'))
        self.menu.append(ui.Info_Text('Recording the raw eye video is optional. We use it for debugging.'))
        self.menu.append(ui.Switch('record_eye', self, on_val=True, off_val=False, label='Record eye'))

        def audio_dev_getter():
            # fetch list of currently available
            self.audio_devices_dict = Audio_Input_Dict()
            devices = list(self.audio_devices_dict.keys())
            return devices, devices
        self.menu.append(ui.Selector('audio_src', self, selection_getter=audio_dev_getter, label='Audio Source'))

        self.button = ui.Thumb('running', self, setter=self.toggle, label='R', hotkey='r')
        self.button.on_color[:] = (1, .0, .0, .8)
        self.g_pool.quickbar.insert(1, self.button)

    def deinit_gui(self):
        if self.menu:
            self.g_pool.sidebar.remove(self.menu)
            self.menu = None
        if self.button:
            self.g_pool.quickbar.remove(self.button)
            self.button = None

    def toggle(self, _=None):
        if self.running:
            self.notify_all({'subject': 'recording.should_stop'})
            self.notify_all({'subject': 'recording.should_stop', 'remote_notify': 'all'})
        else:
            self.notify_all({'subject': 'recording.should_start', 'session_name': self.session_name})
            self.notify_all({'subject': 'recording.should_start', 'session_name': self.session_name, 'remote_notify': 'all'})

    def on_notify(self, notification):
        """Handles recorder notifications

        Reacts to notifications:
            ``recording.should_start``: Starts a new recording session
            ``recording.should_stop``: Stops current recording session

        Emits notifications:
            ``recording.started``: New recording session started
            ``recording.stopped``: Current recording session stopped

        Args:
            notification (dictionary): Notification dictionary
        """
        # notification wants to be recorded
        if notification.get('record', False) and self.running:
            if 'timestamp' not in notification:
                logger.error("Notification without timestamp will not be saved.")
            else:
                self.data['notifications'].append(notification)
        elif notification['subject'] == 'recording.should_start':
            if self.running:
                logger.info('Recording already running!')
            elif not self.g_pool.capture.online:
                logger.error("Current world capture is offline. Please reconnect or switch to fake capture")
            else:
                if notification.get("session_name", ""):
                    self.set_session_name(notification["session_name"])
                self.start()

        elif notification['subject'] == 'recording.should_stop':
            if self.running:
                self.stop()
            else:
                logger.info('Recording already stopped!')

    def get_rec_time_str(self):
        rec_time = gmtime(time()-self.start_time)
        return strftime("%H:%M:%S", rec_time)

    def start(self):
        self.data = {'pupil_positions': [], 'gaze_positions': [], 'notifications': []}
        self.frame_count = 0
        self.running = True
        self.menu.read_only = True
        self.start_time = time()

        session = os.path.join(self.rec_dir, self.session_name)
        try:
            os.makedirs(session)
            logger.debug("Created new recordings session dir {}".format(session))

        except:
            logger.debug("Recordings session dir {} already exists, using it.".format(session))

        # set up self incrementing folder within session folder
        counter = 0
        while True:
            self.rec_path = os.path.join(session, "{:03d}/".format(counter))
            try:
                os.mkdir(self.rec_path)
                logger.debug("Created new recording dir {}".format(self.rec_path))
                break
            except:
                logger.debug("We dont want to overwrite data, incrementing counter & trying to make new data folder")
                counter += 1

        self.meta_info_path = os.path.join(self.rec_path, "info.csv")

        with open(self.meta_info_path, 'w', newline='') as csvfile:
            csv_utils.write_key_value_file(csvfile, {
                'Recording Name': self.session_name,
                'Start Date': strftime("%d.%m.%Y", localtime(self.start_time)),
                'Start Time': strftime("%H:%M:%S", localtime(self.start_time))
            })

        if self.audio_src != 'No Audio':
            audio_path = os.path.join(self.rec_path, "world.wav")
            self.audio_writer = Audio_Capture(audio_path, self.audio_devices_dict[self.audio_src])
        else:
            self.audio_writer = None

        self.video_path = os.path.join(self.rec_path, "world.mp4")
        if self.raw_jpeg and self.g_pool.capture.jpeg_support:
            self.writer = JPEG_Writer(self.video_path, self.g_pool.capture.frame_rate)
        elif hasattr(self.g_pool.capture._recent_frame, 'h264_buffer'):
            self.writer = H264Writer(self.video_path,
                                     self.g_pool.capture.frame_size[0],
                                     self.g_pool.capture.frame_size[1],
                                     self.g_pool.capture.frame_rate)
        else:
            self.writer = AV_Writer(self.video_path, fps=self.g_pool.capture.frame_rate)

        try:
            cal_pt_path = os.path.join(self.g_pool.user_dir, "user_calibration_data")
            cal_data = load_object(cal_pt_path)
            notification = {'subject': 'calibration.calibration_data', 'record': True}
            notification.update(cal_data)
            self.data['notifications'].append(notification)
        except:
            pass

        if self.show_info_menu:
            self.open_info_menu()
        logger.info("Started Recording.")
        self.notify_all({'subject': 'recording.started', 'rec_path': self.rec_path,
                         'session_name': self.session_name, 'record_eye': self.record_eye,
                         'compression': self.raw_jpeg})

    def open_info_menu(self):
        self.info_menu = ui.Growing_Menu('additional Recording Info', size=(300, 300), pos=(300, 300))
        self.info_menu.configuration = self.info_menu_conf

        def populate_info_menu():
            self.info_menu.elements[:-2] = []
            for name in self.user_info.keys():
                self.info_menu.insert(0, ui.Text_Input(name, self.user_info))

        def set_user_info(new_string):
            self.user_info = new_string
            populate_info_menu()

        populate_info_menu()
        self.info_menu.append(ui.Info_Text('Use the *user info* field to add/remove additional fields and their values. The format must be a valid Python dictionary. For example -- {"key":"value"}. You can add as many fields as you require. Your custom fields will be saved for your next session.'))
        self.info_menu.append(ui.Text_Input('user_info', self, setter=set_user_info, label="User info"))
        self.g_pool.gui.append(self.info_menu)

    def close_info_menu(self):
        if self.info_menu:
            self.info_menu_conf = self.info_menu.configuration
            self.g_pool.gui.remove(self.info_menu)
            self.info_menu = None

    def recent_events(self,events):
        if self.running:
            for key, data in events.items():
                if key not in ('dt','frame'):
                    try:
                        self.data[key] += data
                    except KeyError:
                        self.data[key] = []
                        self.data[key] += data

            if 'frame' in events:
                frame = events['frame']
                self.writer.write_video_frame(frame)
                self.frame_count += 1

            # # cv2.putText(frame.img, "Frame %s"%self.frame_count,(200,200), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100))

            self.button.status_text = self.get_rec_time_str()

    def stop(self):
        # explicit release of VideoWriter
        self.writer.release()
        self.writer = None

        save_object(self.data, os.path.join(self.rec_path, "pupil_data"))

        try:
            copy2(os.path.join(self.g_pool.user_dir, "surface_definitions"),
                  os.path.join(self.rec_path, "surface_definitions"))
        except:
            logger.info("No surface_definitions data found. You may want this if you do marker tracking.")

        camera_calibration = load_camera_calibration(self.g_pool)
        if camera_calibration is not None:
            save_object(camera_calibration, os.path.join(self.rec_path, "camera_calibration"))
        else:
            logger.info("No camera calibration found.")

        try:
            with open(self.meta_info_path, 'a', newline='') as csvfile:
                csv_utils.write_key_value_file(csvfile, {
                    'Duration Time': self.get_rec_time_str(),
                    'World Camera Frames': self.frame_count,
                    'World Camera Resolution': str(self.g_pool.capture.frame_size[0])+"x"+str(self.g_pool.capture.frame_size[1]),
                    'Capture Software Version': self.g_pool.version,
                    'Data Format Version': self.g_pool.version,
                    'System Info': get_system_info()
                }, append=True)
        except Exception:
            logger.exception("Could not save metadata. Please report this bug!")

        try:
            with open(os.path.join(self.rec_path, "user_info.csv"), 'w', newline='') as csvfile:
                csv_utils.write_key_value_file(csvfile, self.user_info)
        except Exception:
            logger.exception("Could not save userdata. Please report this bug!")

        self.close_info_menu()

        if self.audio_writer:
            self.audio_writer = None

        self.running = False
        self.menu.read_only = False
        self.button.status_text = ''

        self.data = {'pupil_positions': [], 'gaze_positions': []}
        self.pupil_pos_list = []
        self.gaze_pos_list = []

        logger.info("Saved Recording.")
        self.notify_all({'subject': 'recording.stopped', 'rec_path': self.rec_path})

    def cleanup(self):
        """gets called when the plugin get terminated.
           either volunatily or forced.
        """
        if self.running:
            self.stop()
        self.deinit_gui()

    def verify_path(self, val):
        try:
            n_path = os.path.expanduser(val)
            logger.debug("Expanded user path.")
        except:
            n_path = val
        if not n_path:
            logger.warning("Please specify a path.")
            return False
        elif not os.path.isdir(n_path):
            logger.warning("This is not a valid path.")
            return False
        # elif not os.access(n_path, os.W_OK):
        elif not writable_dir(n_path):
            logger.warning("Do not have write access to '{}'.".format(n_path))
            return False
        else:
            return n_path

    def set_rec_dir(self, val):
        n_path = self.verify_path(val)
        if n_path:
            self.rec_dir = n_path

    def set_session_name(self, val):
        if not val:
            self.session_name = get_auto_name()
        else:
            if os.path.sep in val:
                logger.warning('You session name will create one or more subdirectories')
            self.session_name = val


def writable_dir(n_path):
    try:
        open(os.path.join(n_path, 'dummpy_tmp'), 'w')
    except IOError:
        return False
    else:
        os.remove(os.path.join(n_path, 'dummpy_tmp'))
        return True
