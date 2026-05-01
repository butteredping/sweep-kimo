import time
import random
import subprocess
import struct
import socket
from typing import Optional

import cv2
import numpy as np

import bot.conn.os as os
import bot.base.log as logger
import threading

from bot.base.point import ClickPoint, ClickPointType
from bot.conn.ctrl import AndroidController
from bot.recog.image_matcher import template_match, image_match
from config import CONFIG, Config
from dataclasses import dataclass, field
from module.umamusume.asset.template import REF_DONT_CLICK

log = logger.get_logger(__name__)

INPUT_BLOCKED = False
IN_CAREER_RUN = False

# ─────────────────────────────────────────────────────────────────────────────
# STOP IMAGE DETECTION
#
# This is NOT checked on every frame. Call ctrl.check_stop_at_lobby(screen)
# explicitly from your lobby handler so it only runs at the main lobby screen.
# Limited success so far.
# ─────────────────────────────────────────────────────────────────────────────
STOP_IMAGE_PATH       = "bot/conn/stop_signal.png"
STOP_IMAGE_CONFIDENCE = 0.85   # 0.0–1.0  (lower = more tolerant)

_stop_image_template = None

def _load_stop_template():
    global _stop_image_template
    if _stop_image_template is None:
        try:
            tmpl = cv2.imread(STOP_IMAGE_PATH, cv2.IMREAD_COLOR)
            if tmpl is not None:
                _stop_image_template = tmpl
                log.info(f"[STOP] Loaded stop image from '{STOP_IMAGE_PATH}'")
            else:
                log.warning(f"[STOP] Could not read '{STOP_IMAGE_PATH}' — stop detection disabled.")
        except Exception as e:
            log.warning(f"[STOP] Error loading stop image: {e}")
    return _stop_image_template
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class U2AndroidConfig:
    _device_name: str
    delay: float
    bluestacks_config_path: Optional[str] = None
    bluestacks_config_keyword: Optional[str] = None

    _bluestacks_port: Optional[str] = field(init=False, repr=False, default=None)

    @property
    def device_name(self) -> str:
        bluestacks_port = self.bluestacks_port
        if bluestacks_port is not None:
            return f"127.0.0.1:{bluestacks_port}"
        return self._device_name

    @property
    def bluestacks_port(self) -> Optional[str]:
        if self._bluestacks_port is not None:
            return self._bluestacks_port
        if self.bluestacks_config_path and self.bluestacks_config_keyword:
            with open(self.bluestacks_config_path) as file:
                self._bluestacks_port = next((
                    line.split('=')[1].strip().strip('"')
                    for line in file
                    if self.bluestacks_config_keyword in line
                ), None)
        return self._bluestacks_port

    @staticmethod
    def load(config: Config):
        return U2AndroidConfig(
            _device_name=config.bot.auto.adb.device_name,
            delay=config.bot.auto.adb.delay,
            bluestacks_config_path=config.bot.auto.adb.bluestacks_config_path,
            bluestacks_config_keyword=config.bot.auto.adb.bluestacks_config_keyword,
        )


class U2AndroidController(AndroidController):
    config = U2AndroidConfig.load(CONFIG)

    path = "deps\\adb\\"
    recent_point = None
    recent_operation_time = None
    same_point_operation_interval = 0.15

    repetitive_click_name = None
    repetitive_click_count = 0
    repetitive_other_clicks = 0
    last_click_time = 0.0
    min_click_interval = 0.15

    screen_width = None
    screen_height = None
    screencap_cmd = None
    screencap_lock = None
    pixel_buffer = None
    last_dims = None

    def __init__(self):
        self.recent_click_buckets = []
        self.fallback_block_until = 0.0
        self.trigger_decision_reset = False
        self.last_recovery_time = 0
        self.recovery_grace_until = 0.0
        self.screencap_lock = threading.Lock()
        
        self._cached_frame = None
        self._cache_time = 0.0
        self._cache_max_age = 0.120
        
        transport_cmd = f'host:transport:{self.config.device_name}'
        self._transport_bytes = f'{len(transport_cmd):04x}{transport_cmd}'.encode()
        self._exec_bytes = b'000eexec:screencap'

        self._pool_sock = None
        self._pool_lock = threading.Lock()
        
        try:
            from bot.base.runtime_state import load_persisted
            load_persisted()
        except Exception:
            pass

    def _create_adb_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2097152)
        sock.connect(('127.0.0.1', 5037))
        sock.sendall(self._transport_bytes)
        resp = sock.recv(4)
        if not resp or b'OKAY' not in resp:
            sock.close()
            return None
        return sock

    def _close_pool_sock(self):
        if self._pool_sock is not None:
            try:
                self._pool_sock.close()
            except Exception:
                pass
            self._pool_sock = None

    def _capture_via_socket(self):
        with self._pool_lock:
            sock = self._pool_sock
            self._pool_sock = None

        if sock is not None:
            raw = self._exec_screencap(sock)
            if raw is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                self._prefill_pool()
                return raw
            try:
                sock.close()
            except Exception:
                pass

        try:
            sock = self._create_adb_socket()
            if sock is None:
                return None
            raw = self._exec_screencap(sock)
            try:
                sock.close()
            except Exception:
                pass
            if raw is not None:
                self._prefill_pool()
            return raw
        except Exception:
            return None

    def _exec_screencap(self, sock):
        try:
            sock.sendall(self._exec_bytes)
            resp = sock.recv(4)
            if not resp or b'OKAY' not in resp:
                return None
            chunks = []
            while True:
                chunk = sock.recv(1048576)
                if not chunk:
                    break
                chunks.append(chunk)
            return b''.join(chunks) if chunks else None
        except Exception:
            return None

    def _prefill_pool(self):
        def _fill():
            try:
                sock = self._create_adb_socket()
                if sock is None:
                    return
                with self._pool_lock:
                    if self._pool_sock is not None:
                        sock.close()
                    else:
                        self._pool_sock = sock
            except Exception:
                pass
        threading.Thread(target=_fill, daemon=True).start()

    def screencap(self):
        now = time.time()
        if self._cached_frame is not None and (now - self._cache_time) < self._cache_max_age:
            return self._cached_frame
        
        for attempt in range(3):
            raw = self._capture_via_socket()
            
            if raw is None or len(raw) < 16:
                time.sleep(0.1)
                continue
            
            try:
                w, h, fmt = struct.unpack('<III', raw[:12])
                if w <= 0 or h <= 0 or w > 10000 or h > 10000:
                    time.sleep(0.1)
                    continue
                
                pixel_size = w * h * 4
                header_size = 16 if len(raw) >= 16 + pixel_size else 12
                
                if len(raw) < header_size + pixel_size:
                    time.sleep(0.1)
                    continue
                
                img = np.frombuffer(raw[header_size:header_size + pixel_size], dtype=np.uint8).reshape((h, w, 4))
                
                if fmt == 5:
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                else:
                    img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
                
                self._cached_frame = img
                self._cache_time = time.time()
                
                return img
            except Exception:
                time.sleep(0.1)
                continue
        
        self._cached_frame = None
        return None

### stop-image check ###
    def check_stop_at_lobby(self, screen_bgr=None) -> None:
"""
        Call this at the top of your lobby recognition handler, e.g.:
            self.ctrl.check_stop_at_lobby(screen)
 If the stop PNG is found on screen the bot logs and exits cleanly.
        If screen_bgr is None, a fresh screencap is taken automatically.
"""
        img = screen_bgr if screen_bgr is not None else self.get_screen()
        if img is None:
            return
        tmpl = _load_stop_template()
        if tmpl is None:
            return
        try:
            result = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val >= STOP_IMAGE_CONFIDENCE:
                log.info(f"[STOP] Stop image matched at lobby (confidence={max_val:.2f}) — exiting bot.")
                import sys
                sys.exit(0)
        except Exception as e:
            log.warning(f"[STOP] Template match error: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    def in_fallback_block(self, name):
        if isinstance(name, str) and name == "Default fallback click":
            if time.time() < getattr(self, "fallback_block_until", 0.0):
                return True
        return False

    def update_click_buckets(self, x, y):
        bucket = (int(x/25), int(y/25))
        lst = getattr(self, "recent_click_buckets", None)
        if lst is None:
            self.recent_click_buckets = []
            lst = self.recent_click_buckets
        if bucket not in lst:
            lst.append(bucket)
            if len(lst) > 2:
                lst.pop(0)
            self.fallback_block_until = time.time() + 2.0

    def build_click_key(self, x, y, name):
        if isinstance(name, str) and name.strip() != "":
            return name.strip()
        return f"{int(x/50)}:{int(y/50)}"

    def update_repetitive_click(self, click_key):
        try:
            from bot.base.runtime_state import update_repetitive, get_repetitive_threshold
            repetitive_threshold = int(get_repetitive_threshold())
        except Exception:
            repetitive_threshold = 11
            update_repetitive = None

        if isinstance(click_key, str):
            click_key = click_key.strip()

        if self.repetitive_click_name is None:
            self.repetitive_click_name = click_key
            self.repetitive_click_count = 1
            self.repetitive_other_clicks = 0
            try:
                if update_repetitive:
                    update_repetitive(self.repetitive_click_count, self.repetitive_other_clicks)
            except Exception:
                pass
            return False

        current_name = self.repetitive_click_name.strip() if isinstance(self.repetitive_click_name, str) else self.repetitive_click_name
        is_same_key = (click_key == current_name) or (
            isinstance(click_key, str) and isinstance(current_name, str) and
            click_key.lower() == current_name.lower()
        )

        if is_same_key:
            self.repetitive_click_count += 1
        else:
            self.repetitive_other_clicks += 1
            if self.repetitive_other_clicks >= 2:
                self.repetitive_click_name = click_key
                self.repetitive_click_count = 1
                self.repetitive_other_clicks = 0
        try:
            if update_repetitive:
                update_repetitive(self.repetitive_click_count, self.repetitive_other_clicks)
        except Exception:
            pass

        if time.time() < getattr(self, 'recovery_grace_until', 0.0):
            self.repetitive_click_name = None
            self.repetitive_click_count = 0
            self.repetitive_other_clicks = 0
            return False

        if self.repetitive_click_name == click_key and self.repetitive_click_count >= repetitive_threshold:
            try:
                self.recover_home_and_reopen()
            finally:
                self.repetitive_click_name = None
                self.repetitive_click_count = 0
                self.repetitive_other_clicks = 0
                try:
                    if update_repetitive:
                        update_repetitive(0, 0)
                except Exception:
                    pass
            time.sleep(self.config.delay)
            return True
        return False

    def safety_dont_click(self, x, y):
        if 263 <= x <= 458 and 559 <= y <= 808:
            screen_gray = self.get_screen(to_gray=True)
            match = image_match(screen_gray, REF_DONT_CLICK)
            if getattr(match, "find_match", False):
                log.info("unsafe click blocked")
                return True
        return False

    def randomize_and_clamp(self, x, y, random_offset, max_x, max_y):
        if random_offset:
### pixel sample radius update
            x += int(max(-10, min(8, random.gauss(0, 5))))
            y += int(max(-10, min(10, random.gauss(0, 4))))
        if x >= max_x:
            x = max_x-1
        if y >= max_y:
            y = max_y-1
        if x < 0:
            x = 1
        if y <= 0:
            y = 1
        return x, y

    def wait_click_interval(self, name):
        now = time.time()
        elapsed = now - self.last_click_time if hasattr(self, "last_click_time") else now
        min_interval = random.uniform(0.06, 0.09)
        wait_needed = max(0.0, min_interval - elapsed)
        log.debug(f"click queue: elapsed={elapsed:.3f}s, min_interval={min_interval:.3f}s, wait={wait_needed:.3f}s, name={name}")
        if wait_needed > 0:
            time.sleep(wait_needed)

### add chance for afk pause
    def _maybe_afk_pause(self):
        if random.random() < 0.03:
            afk_dur = random.uniform(4.0, 42.0)
            log.info(f"[AFK] Simulating idle pause for {afk_dur:.1f}s")
            time.sleep(afk_dur)
###

    def tap(self, x, y, hold_duration):
        duration = int(max(50, min(180, random.gauss(90, 30)))) + hold_duration
        drift_x = x + random.randint(-3, 4)
        drift_y = y + random.randint(-3, 7)
        _ = self.execute_adb_shell(f"shell input swipe {x} {y} {drift_x} {drift_y} {duration}", True)
        self.last_click_time = time.time()

### post-tap delay
        time.sleep(self.config.delay * random.uniform(0.3, 1.5))
        if not IN_CAREER_RUN:
            time.sleep(random.uniform(0.3, 0.9))
###

### 3% AFK part 2 pause because I'm not sure where the fuck it goes
        self._maybe_afk_pause()
###

    def init_env(self) -> None:
        try:
            result = subprocess.run(
                [self.path + "adb.exe", "-s", self.config.device_name, "shell", "echo", "ok"],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                raise Exception(f"ADB connection failed: {result.stderr.decode()}")
            log.debug(f"ADB connection verified for {self.config.device_name}")
        except Exception as e:
            log.error(f"Failed to connect to device {self.config.device_name}: {e}")
            raise

    def reinit_connection(self):
        self._close_pool_sock()
        try:
            subprocess.run([self.path + "adb.exe", "-s", self.config.device_name, "reconnect"], 
                          capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(0.2)
        self._screen_width = None
        self._screen_height = None
        self._cached_frame = None
        self._cache_time = 0.0
        self.init_env()

    def get_screen(self, to_gray=False):
        with self.screencap_lock:
            img = self.screencap()
        if img is None:
            return None
        if to_gray:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img

# ===== ctrl =====
    def click_by_point(self, point: ClickPoint, random_offset=True, hold_duration=0):
        if INPUT_BLOCKED:
            return
        if self.recent_point is not None:
            if self.recent_point == point and time.time() - self.recent_operation_time < self.same_point_operation_interval:
                log.warning("request for a same point too frequently")
                return
        if point.target_type == ClickPointType.CLICK_POINT_TYPE_COORDINATE:
            self.click(point.coordinate.x, point.coordinate.y, name=point.desc, random_offset=random_offset, hold_duration=hold_duration)
        elif point.target_type == ClickPointType.CLICK_POINT_TYPE_TEMPLATE:
            cur_screen = self.get_screen(to_gray=True)
            match_result = image_match(cur_screen, point.template)
            if getattr(match_result, "find_match", False):
                self.click(match_result.center_point[0], match_result.center_point[1], name=point.desc, random_offset=random_offset, hold_duration=hold_duration)
        self.recent_point = point
        self.recent_operation_time = time.time()

    def click(self, x, y, name="", random_offset=True, max_x=720, max_y=1280, hold_duration=0):
        if INPUT_BLOCKED:
            return
        if name != "":
            log.debug("click >> " + name)

        if self.in_fallback_block(name):
            return
        self.update_click_buckets(x, y)

        click_key = self.build_click_key(x, y, name)
        if self.update_repetitive_click(click_key):
            return

        try:
            if self.safety_dont_click(x, y):
                return
        except Exception as e:
            log.info("wtf")

        x, y = self.randomize_and_clamp(x, y, random_offset, max_x, max_y)
        
        self.wait_click_interval(name)
        self.tap(x, y, hold_duration)

### check this section next
    def swipe(self, x1=1025, y1=550, x2=1025, y2=550, duration=0.2, name=""):
        if INPUT_BLOCKED:
            return
        if name != "":
            log.debug("swipe >> " + name)
        
        x1 += int(max(-10, min(10, random.gauss(0, 4))))
        y1 += int(max(-10, min(10, random.gauss(0, 4))))
        x2 += int(max(-10, min(10, random.gauss(0, 4))))
        y2 += int(max(-10, min(10, random.gauss(0, 4))))
        
        duration = int(duration * random.uniform(0.94, 1.06))
        
        _ = self.execute_adb_shell(f"shell input swipe {x1} {y1} {x2} {y2} {duration}", True)

### post-swipe delay
        time.sleep(self.config.delay * random.uniform(0.7, 1.5))
###

### 3% AFK pause again
        self._maybe_afk_pause()
###

    def swipe_and_hold(self, x1, y1, x2, y2, swipe_duration, hold_duration, name=""):
        if INPUT_BLOCKED:
            return
        
        x1 += int(max(-8, min(10, random.gauss(0, 4))))
        y1 += int(max(-10, min(12, random.gauss(0, 4))))
        x2 += int(max(-8, min(12, random.gauss(0, 4))))
        y2 += int(max(-10, min(10, random.gauss(0, 3))))
        
        swipe_duration = int(swipe_duration * random.uniform(0.94, 1.08))
        hold_duration = int(hold_duration * random.uniform(0.94, 1.06))
        
        reverse_y = y2 - 28 if y2 > y1 else y2 + 28
        
        _ = self.execute_adb_shell("shell input swipe " + str(x1) + " " + str(y1) + " " + str(x2) + " " + str(y2) + " " + str(swipe_duration), True)
        _ = self.execute_adb_shell("shell input swipe " + str(x2) + " " + str(y2) + " " + str(x2) + " " + str(reverse_y) + " " + str(hold_duration), True)
        
### randomised delays
        time.sleep(self.config.delay * random.uniform(0.7, 1.5))
###

### 3% AFK pause
        self._maybe_afk_pause()
###

# ===== common device/adb commands =====

# execute_adb_shell command
    def execute_adb_shell(self, cmd, sync):
        cmd_str = self.path + "adb -s " + self.config.device_name + " " + cmd
        proc = os.run_cmd(cmd_str)
        if sync:
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                log.error(f"ADB command timed out: {cmd_str}")
                proc.kill()
                proc.communicate()
        else:
            def _wait():
                try:
                    proc.communicate(timeout=60)
                except Exception:
                    proc.kill()
            threading.Thread(target=_wait, daemon=True).start()
        return proc

    def recover_home_and_reopen(self):
        if time.time() - self.last_recovery_time < 25:
            return
        self.last_recovery_time = time.time()
        self.recovery_grace_until = time.time() + 60
        try:
            log.info("rannnnn")
            for _ in range(3):
                self.execute_adb_shell("shell input keyevent 4", True)
                time.sleep(0.6)
            self.execute_adb_shell("shell input keyevent 3", True)
            time.sleep(0.8)
        except Exception:
            pass
        try:
            self.execute_adb_shell("shell monkey -p com.cygames.umamusume -c android.intent.category.LAUNCHER 1", True)
            time.sleep(1.6)
        except Exception:
            pass
        self.trigger_decision_reset = True

    def start_app(self, package_name, activity_name=None):
        if activity_name:
            component = f"{package_name}/{activity_name}"
            cmd = f"shell am start -n {component}"
            self.execute_adb_shell(cmd, True)
            log.debug("starting app using ADB: " + component)
        else:
            cmd = f"shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
            self.execute_adb_shell(cmd, True)
            log.debug("starting app using ADB: " + package_name)

# get_front_activity; get application currently running in foreground
    def get_front_activity(self):
        rsp = self.execute_adb_shell("shell \"dumpsys window windows | grep \"Current\"\"", True).communicate()
        log.debug(str(rsp))
        return str(rsp)

# get_devices adb connection status
    def get_devices(self):
        p = os.run_cmd(self.path + "adb devices").communicate()
        devices = p[0].decode()
        log.debug(devices)
        return devices

# connect_to_device
    def connect_to_device(self):
        p = os.run_cmd(self.path + "adb connect " + self.config.device_name).communicate()
        log.debug(p[0].decode())

    # kill_adb_server; stop adb-server
    def kill_adb_server(self):
        p = os.run_cmd(self.path + "adb kill-server").communicate()
        log.debug(p[0].decode())

# check if file exists
    def check_file_exist(self, file_path, file_name):
        rsp = self.execute_adb_shell("shell ls " + file_path, True).communicate()
        file_list = rsp[0].decode()
        log.debug(str("ls file result:" + file_list))
        return file_name in file_list

# push_file
    def push_file(self, src, dst):
        self.execute_adb_shell("push " + src + " " + dst, True)

# get_device_os_info
    def get_device_os_info(self):
        rsp = self.execute_adb_shell("shell getprop ro.build.version.sdk", True).communicate()
        os_info = rsp[0].decode().replace('\r', '').replace('\n', '')
        log.debug("device os info: " + os_info)
        return os_info

# get_device_cpu_info
    def get_device_cpu_info(self):
        rsp = self.execute_adb_shell("shell getprop ro.product.cpu.abi", True).communicate()
        cpu_info = rsp[0].decode().replace('\r', '').replace('\n', '')
        log.debug("device cpu info: " + cpu_info)
        return cpu_info

def destroy(self):
        self._cached_frame = None
        self._close_pool_sock()
