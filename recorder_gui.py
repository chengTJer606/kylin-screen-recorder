#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
录屏工具 · GUI 版（麒麟 UKUI 实测可用）
功能：全屏录制、系统声音/麦克风/混音、暂停/继续、停止合成；
      输出目录可选、时间戳自动命名、收尾弹窗（重命名/打开/播放）；
      开始后自动最小化、3秒倒计时、画质三档、磁盘空间守护
原理：视频 sudo ffmpeg kmsgrab 独立进程；音频 用户态 ffmpeg pulse 独立进程；
      暂停=分段法（停当前段/新起段）；停止=concat 合并各段后合成 mp4
"""
import glob
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, messagebox, simpledialog, ttk

# ---------------- 路径与常量 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "录像")
SEG_DIR = os.path.join(BASE_DIR, ".segments")
CFG_FILE = os.path.join(BASE_DIR, "recorder_config.json")
FPS = 30

AUDIO_MODES = {
    "none":   "无声音",
    "system": "电脑声音",
    "mic":    "麦克风",
    "both":   "电脑+麦克风",
}
QUALITY = {          # 显示名 -> crf
    "流畅": 28,
    "高清": 23,
    "原画": 18,
}
DISK_GUARD_BYTES = 1 * 1024 ** 3   # 磁盘剩余低于 1GB 触发守护
DISK_CHECK_INTERVAL = 30           # 录制中每 30 秒检查一次
BAD_CHARS = '/\\:*?"<>|'
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_KEEP_REC = 20                  # 每次录制的 ffmpeg 日志保留最近 20 次

log = logging.getLogger("recorder")


def setup_logging():
    """初始化 logs/ 日志：app.log 滚动保存（5MB×3）"""
    os.makedirs(LOG_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"),
                            maxBytes=5 * 1024 ** 2, backupCount=3,
                            encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)


def log_session_boot():
    """会话启动快照：版本与默认音频设备（排查问题的第一手上下文）"""
    log.info("=" * 60)
    log.info("会话启动")
    try:
        v = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True,
                           timeout=5).stdout.splitlines()
        if v:
            log.info("ffmpeg: %s", v[0])
    except Exception:
        log.warning("无法获取 ffmpeg 版本")
    log.info("默认输出: %s", pactl_field(["默认音频入口", "Default Sink"]) or "未知")
    log.info("默认输入: %s", pactl_field(["默认信源", "Default Source"]) or "未知")


# ---------------- 工具函数 ----------------
def load_config():
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CFG_FILE, 0o600)


_SESSION_PW = None   # 会话内存缓存：进程退出即消失，密码永不写入磁盘


def get_password(parent=None):
    """每次启动工具弹窗询问一次，本次运行内复用（不落盘）"""
    global _SESSION_PW
    if _SESSION_PW:
        return _SESSION_PW
    pw = simpledialog.askstring(
        "需要管理员密码",
        "录屏需要管理员权限（抓屏接口需要 root）。\n请输入登录密码（仅本次运行有效，不会保存）：",
        show="*", parent=parent)
    if pw:
        _SESSION_PW = pw
    return pw


def sudo_run(cmd, pw, timeout=15):
    p = subprocess.run(["sudo", "-S", "-p", "", "bash", "-c", cmd],
                       input=pw + "\n", text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout.strip()


def pactl_field(patterns):
    try:
        out = subprocess.run(["pactl", "info"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=5).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        for pat in patterns:
            if pat in line:
                for sep in ("：", ":"):
                    if sep in line:
                        return line.split(sep, 1)[1].strip()
    return ""


def default_monitor():
    sink = pactl_field(["默认音频入口", "Default Sink"])
    return (sink + ".monitor") if sink else ""


def default_mic():
    return pactl_field(["默认信源", "Default Source"])


def enum_input_sources():
    """枚举全部真实输入源（排除 monitor 监听源），返回 [(名称, 描述)]。
    兼容 pactl 中英文输出（名称/Name、描述/Description）"""
    try:
        out = subprocess.run(["pactl", "list", "sources"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=8).stdout
    except Exception:
        return []
    sources, name, desc = [], "", ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("名称：") or s.startswith("Name:"):
            name = re.split(r"：|:", s, maxsplit=1)[1].strip()
        elif s.startswith("描述：") or s.startswith("Description:"):
            desc = re.split(r"：|:", s, maxsplit=1)[1].strip()
            if name and not name.endswith(".monitor"):
                sources.append((name, desc or name))
            name, desc = "", ""
    return sources


def source_exists(name):
    """探测输入源当前是否存在（蓝牙未连接时其 source 不在列表中）"""
    try:
        out = subprocess.run(["pactl", "list", "sources", "short"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=6).stdout
        return any(len(cols) > 1 and cols[1] == name
                   for cols in (ln.split("\t") for ln in out.splitlines()))
    except Exception:
        return False


def ensure_analog_headset():
    """自愈：若默认输出是USB耳机却被系统错配为IEC958数字输出
    （此状态下声音流不通，任何录屏软件都录不到），自动切回模拟立体声。
    蓝牙(A2DP)/HDMI/板载均不动。返回修正描述或 None"""
    sink = pactl_field(["默认音频入口", "Default Sink"])
    if "iec958" not in sink.lower():
        return None
    m = re.match(r"alsa_output\.(.+)\.iec958", sink)
    if not m:
        return None
    card_id = m.group(1)
    if not re.search(r"usb|headset|headphone", card_id, re.I):
        return None
    card = "alsa_card." + card_id
    for prof in ("output:analog-stereo+input:mono-fallback", "output:analog-stereo"):
        p = subprocess.run(["pactl", "set-card-profile", card, prof],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=6)
        if p.returncode == 0:
            new_sink = pactl_field(["默认音频入口", "Default Sink"])
            if "iec958" not in new_sink.lower():
                return "检测到耳机被错配为数字输出(IEC958)，已自动切换为模拟输出"
    return None


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return "{:02d}:{:02d}:{:02d}".format(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def notify(title, msg):
    try:
        subprocess.Popen(["notify-send", title, msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def timestamp_name():
    """默认文件名：录屏_20261019_113025（统一下划线）"""
    return time.strftime("录屏_%Y%m%d_%H%M%S")


# ---------------- 录制引擎 ----------------
class Engine:
    def __init__(self, password):
        self.pw = password
        self.segments = []
        self.seg_index = 0
        self.cur = None
        self.seg_start_mono = None
        self.done_seconds = 0.0
        self.last_error = ""
        self.warning = ""          # 非致命提示（音频自愈/静音自检）
        self.time_offset = 0.0     # 首段倒计时偏移（界面计时从倒计时结束清零起算）

    def _warn(self, msg):
        """非致命提示追加（多处告警不互相覆盖）"""
        self.warning = (self.warning + "；" + msg) if self.warning else msg
        log.warning(msg)

    def _launch_segment(self, audio_mode, crf, region=None, mic_device=None):
        self.seg_index += 1
        log.info("启动第 %d 段: 声音=%s crf=%s 区域=%s 指定麦克风=%s",
                 self.seg_index, audio_mode, crf, region, mic_device or "跟随默认")
        os.makedirs(SEG_DIR, exist_ok=True)
        v_file = os.path.join(SEG_DIR, "seg_{:03d}_v.mp4".format(self.seg_index))
        a_file = os.path.join(SEG_DIR, "seg_{:03d}_a.m4a".format(self.seg_index))
        pid_file = os.path.join(SEG_DIR, "seg_{:03d}.pid".format(self.seg_index))
        log_file = os.path.join(SEG_DIR, "seg_{:03d}_v.log".format(self.seg_index))

        # 编码参数说明（本机实测定案，勿轻易改）:
        # 1) 不加 fps 滤镜: 该滤镜在"后台+SIGINT停止"模式下会丢失队列内容
        #    (实测6秒只剩1.8秒); 默认cfr会按-framerate把96Hz采集丢帧到30fps,
        #    帧间隔实测完全均匀(0.034s)
        # 2) preset 必须 ultrafast: 96fps涌入时 veryfast 编码跟不上会丢帧
        vf = "hwdownload,format=bgr0"
        if region:
            rx, ry, rw, rh = region
            vf += ",crop={}:{}:{}:{}".format(rw, rh, rx, ry)
        vcmd = ("nohup ffmpeg -hide_banner -loglevel warning -y "
                "-f kmsgrab -framerate {fps} -i - "
                "-vf '{vfilter}' "
                "-c:v libx264 -preset ultrafast -crf {crf} -pix_fmt yuv420p -profile:v high "
                "-movflags +faststart '{vf}' > '{log}' 2>&1 & echo $! > '{pid}'"
                ).format(fps=FPS, vfilter=vf, crf=crf, vf=v_file, log=log_file, pid=pid_file)
        rc, _ = sudo_run(vcmd, self.pw)
        if rc != 0:
            self.last_error = "视频进程启动失败（sudo 失败）"
            return False
        v_pid = None
        for _ in range(20):
            rc, out = sudo_run("cat '{}' 2>/dev/null".format(pid_file), self.pw)
            if out.isdigit():
                v_pid = int(out)
                break
            time.sleep(0.1)
        if not v_pid:
            self.last_error = "无法获取视频进程 PID"
            return False

        a_proc = None
        if audio_mode != "none":
            # 自愈：USB耳机被错配为IEC958数字输出时自动切回模拟（否则必录不到）
            notice = ensure_analog_headset()
            if notice:
                self._warn(notice)
            mon = default_monitor()
            mic = default_mic()
            if mic_device:
                # 用户指定了麦克风：先探测存在（蓝牙未连接时不在源列表中）
                if source_exists(mic_device):
                    mic = mic_device
                    if "bluez" in mic_device.lower():
                        self._warn("本次使用蓝牙麦克风，录音为电话音质（HFP 协议限制）")
                else:
                    self._warn("所选麦克风当前不存在（蓝牙未连接？），已回落到系统默认输入")
            log.info("音频源: monitor=%s mic=%s", mon, mic)
            if audio_mode == "system" and mon:
                args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-f", "pulse", "-i", mon, "-c:a", "aac", "-b:a", "128k", a_file]
            elif audio_mode == "mic" and mic:
                args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-f", "pulse", "-i", mic, "-c:a", "aac", "-b:a", "128k", a_file]
            elif audio_mode == "both" and mon and mic:
                args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-f", "pulse", "-i", mon, "-f", "pulse", "-i", mic,
                        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest[a]",
                        "-map", "[a]", "-c:a", "aac", "-b:a", "128k", a_file]
            else:
                args = None
                self.last_error = "未找到可用音频源，本段仅录画面"
            if args:
                a_log = open(os.path.join(SEG_DIR, "seg_{:03d}_a.log".format(self.seg_index)), "w")
                a_proc = subprocess.Popen(args, stdout=a_log, stderr=a_log,
                                          stdin=subprocess.DEVNULL)

        self.cur = {"v_pid": v_pid, "a_proc": a_proc, "v_file": v_file,
                    "a_file": a_file if a_proc else None}
        # 等待视频首帧写入（计时起点）；同一循环内并行监测音频首字节，
        # 以便 F2 计算音频超前量（音频进程启动比视频快约1秒）
        v_t0 = None
        a_t0 = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            now = time.monotonic()
            if v_t0 is None and os.path.exists(v_file) and os.path.getsize(v_file) > 0:
                v_t0 = now
            if a_proc is not None and a_t0 is None and \
               os.path.exists(a_file) and os.path.getsize(a_file) > 0:
                a_t0 = now
            if v_t0 is not None and (a_proc is None or a_t0 is not None):
                break
            time.sleep(0.05)
        if v_t0 is None:
            v_t0 = time.monotonic()
        if a_proc is not None and a_t0 is None:
            a_t0 = time.monotonic()
        self.cur["v_t0"] = v_t0          # 视频有效起点（首帧时刻）
        self.cur["a_t0"] = a_t0          # 音频有效起点（首字节时刻）
        self.seg_start_mono = v_t0
        log.info("第 %d 段就绪: v_pid=%s 音频超前量=%.2fs",
                 self.seg_index, v_pid,
                 (v_t0 - a_t0) if (a_t0 and v_t0) else -1)
        return True

    def _finish_segment(self):
        if not self.cur:
            return
        dur = time.monotonic() - self.seg_start_mono
        pid = self.cur["v_pid"]
        sudo_run("kill -INT {} 2>/dev/null".format(pid), self.pw)
        dead = False
        for _ in range(24):
            rc, _ = sudo_run("kill -0 {} 2>/dev/null".format(pid), self.pw)
            if rc != 0:
                dead = True
                break
            time.sleep(0.5)
        if not dead:
            # 兜底：SIGINT 未响应则强杀，防止僵尸进程长期占用抓屏通道
            # （曾出现残留进程导致后续所有录制丢帧的事故）
            sudo_run("kill -TERM {} 2>/dev/null".format(pid), self.pw)
            time.sleep(1)
            sudo_run("kill -9 {} 2>/dev/null".format(pid), self.pw)
            self.last_error = "警告：一段录制未正常收尾，已强制结束（该段尾部可能略短）"
        ap = self.cur["a_proc"]
        if ap and ap.poll() is None:
            try:
                ap.send_signal(signal.SIGINT)
                ap.wait(timeout=8)
            except Exception:
                ap.kill()
        # F2: 音频超前量 = 视频首帧时刻 - 音频首字节时刻（音频启动快，通常>0）
        v_t0 = self.cur.get("v_t0", 0.0)
        a_t0 = self.cur.get("a_t0")
        a_delay = max(0.0, v_t0 - a_t0) if (a_t0 and v_t0) else 0.0
        seg = {"v": self.cur["v_file"], "a": self.cur["a_file"], "dur": dur,
               "a_delay": a_delay}
        if os.path.exists(seg["v"]) and os.path.getsize(seg["v"]) > 0:
            self.segments.append(seg)
            self.done_seconds += dur
            log.info("第 %d 段完成: 时长 %.1fs", self.seg_index, dur)
        else:
            log.error("第 %d 段视频为空，已丢弃", self.seg_index)
        self.cur = None
        self.seg_start_mono = None

    def start(self, audio_mode, crf, region=None, mic_device=None):
        if self.cur:
            return False
        return self._launch_segment(audio_mode, crf, region, mic_device)

    def pause(self):
        log.info("暂停（结束当前段）")
        self._finish_segment()

    def resume(self, audio_mode, crf, region=None, mic_device=None):
        log.info("继续（新起一段）")
        return self._launch_segment(audio_mode, crf, region, mic_device)

    def elapsed(self):
        """界面显示计时：扣除首段倒计时偏移，从倒计时结束清零起算"""
        t = self.done_seconds
        if self.seg_start_mono is not None:
            t += time.monotonic() - self.seg_start_mono
        return max(0.0, t - self.time_offset)

    def _archive_logs(self, tag):
        """把本次各段的 ffmpeg 日志归并保存到 logs/（免截图排查），
        仅保留最近 LOG_KEEP_REC 次录制的归档"""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            for kind in ("v", "a"):
                parts = sorted(glob.glob(os.path.join(
                    SEG_DIR, "seg_*_%s.log" % kind)))
                if not parts:
                    continue
                dst = os.path.join(LOG_DIR, "rec_%s_%s.log" % (tag, kind))
                with open(dst, "w", encoding="utf-8") as out:
                    for p in parts:
                        out.write("===== %s =====\n" % os.path.basename(p))
                        try:
                            with open(p, encoding="utf-8", errors="replace") as f:
                                out.write(f.read())
                        except Exception:
                            pass
                        out.write("\n")
            olds = sorted(glob.glob(os.path.join(LOG_DIR, "rec_*_?.log")),
                          key=os.path.getmtime)[:-LOG_KEEP_REC * 2]
            for p in olds:
                os.remove(p)
        except Exception:
            log.exception("ffmpeg 日志归档失败")

    def merge(self, out_file):
        """合并所有段 → out_file；返回输出路径或 None"""
        log.info("开始合成: %s", out_file)
        self._finish_segment()
        if not self.segments:
            self.last_error = "没有可合成的录像段"
            log.error(self.last_error)
            return None
        segs, self.segments = self.segments, []
        self.done_seconds = 0.0
        has_audio = any(s["a"] and os.path.exists(s["a"]) and os.path.getsize(s["a"]) > 0
                        for s in segs)
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        # 同名保险：自动加 _backup_时间戳（下划线风格）
        if os.path.exists(out_file):
            stem, ext = os.path.splitext(out_file)
            bak = "{}_backup_{}{}".format(stem, time.strftime("%Y%m%d_%H%M%S"), ext)
            try:
                os.rename(out_file, bak)
            except Exception:
                pass
        if len(segs) == 1:
            v_all = segs[0]["v"]
        else:
            lst = os.path.join(SEG_DIR, "vlist.txt")
            with open(lst, "w") as f:
                for s in segs:
                    f.write("file '{}'\n".format(s["v"]))
            v_all = os.path.join(SEG_DIR, "merged_v.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat",
                            "-safe", "0", "-i", lst, "-c", "copy", v_all],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if not os.path.exists(v_all):
            self.last_error = "视频合并失败"
            return None
        a_all = None
        if has_audio:
            # F2: 逐段裁掉音频超前量（a_delay），使每段音画同时起步
            a_segs = []
            for i, s in enumerate(segs):
                if not (s["a"] and os.path.exists(s["a"])):
                    continue
                delay = s.get("a_delay", 0.0)
                if delay > 0.15:   # 超前量>150ms才裁，避免无意义操作
                    cut = os.path.join(SEG_DIR, "acut_{:03d}.m4a".format(i))
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss",
                                    "{:.3f}".format(delay), "-i", s["a"],
                                    "-c", "copy", cut],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=60)
                    if os.path.exists(cut) and os.path.getsize(cut) > 0:
                        a_segs.append(cut)
                        continue
                a_segs.append(s["a"])
            if len(a_segs) == 1:
                a_all = a_segs[0]
            elif a_segs:
                lst = os.path.join(SEG_DIR, "alist.txt")
                with open(lst, "w") as f:
                    for a in a_segs:
                        f.write("file '{}'\n".format(a))
                a_all = os.path.join(SEG_DIR, "merged_a.m4a")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat",
                                "-safe", "0", "-i", lst, "-c", "copy", a_all],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if a_all and os.path.exists(a_all):
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", v_all, "-i", a_all,
                   "-c", "copy", "-shortest", "-movflags", "+faststart", out_file]
        else:
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", v_all,
                   "-c", "copy", "-movflags", "+faststart", out_file]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        # 归档本次 ffmpeg 日志到 logs/ 后再清理分段（排查问题不再依赖截图）
        self._archive_logs(os.path.splitext(os.path.basename(out_file))[0])
        for f in glob.glob(os.path.join(SEG_DIR, "seg_*")) + \
                 glob.glob(os.path.join(SEG_DIR, "merged_*")) + \
                 glob.glob(os.path.join(SEG_DIR, "acut_*")) + \
                 glob.glob(os.path.join(SEG_DIR, "*list.txt")):
            try:
                os.remove(f)
            except Exception:
                pass
        if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            log.info("合成成功: %s（%.1f MB）", out_file,
                     os.path.getsize(out_file) / 1024 / 1024)
            # 静音自检：选了声音但全程近静音（<-60dB）时给出醒目提示
            if has_audio:
                try:
                    vd = subprocess.run(
                        ["ffmpeg", "-i", out_file, "-af", "volumedetect",
                         "-f", "null", "-"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, timeout=60).stderr
                    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", vd)
                    if m:
                        log.info("静音自检 mean_volume: %s dB", m.group(1))
                        if float(m.group(1)) < -60:
                            self._warn("本次录到的声音几乎全程静音。"
                                       "若选择了电脑声音/麦克风，请检查系统音量、"
                                       "默认输出设备及声卡配置")
                except Exception:
                    log.exception("静音自检执行失败")
            return out_file
        self.last_error = "合成输出失败"
        log.error("合成输出失败: %s", out_file)
        return None


# ---------------- 区域框选（P3：拖框选录制区域） ----------------
def physical_screen_size():
    """读取 DRM 当前显示模式（物理分辨率，无需root），失败返回 None"""
    for modes in sorted(glob.glob("/sys/class/drm/card*-*/modes")):
        try:
            with open(modes) as f:
                first = f.readline().strip()
            if "x" in first:
                w, h = first.lower().split("x")[:2]
                return int(w), int(h)
        except Exception:
            continue
    return None


def capture_background(pw):
    """框选前定格当前桌面画面（绕开 XWayland 下半透明失效导致的黑屏），
    返回截图路径，失败返回 None"""
    path = os.path.join(tempfile.gettempdir(), "rec_pick_bg.png")
    try:
        os.remove(path)
    except OSError:
        pass
    cmd = ("ffmpeg -v error -y -f kmsgrab -i - "
           "-vf 'hwdownload,format=bgr0' -vframes 1 '{}'".format(path))
    try:
        sudo_run(cmd, pw, timeout=25)
    except Exception:
        return None
    return path if (os.path.exists(path) and os.path.getsize(path) > 0) else None


def enum_windows():
    """枚举 X 窗口树（xwininfo），返回 [(x,y,w,h,name), ...]。
    列表顺序：最上层窗口在前（悬停匹配时优先命中）。
    过滤：过小/屏外/框选层自身/录屏主窗。"""
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=6).stdout
    except Exception:
        return []
    geo_re = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")
    wins = []
    for line in out.splitlines():
        # 顶层窗口行：恰好 5 个空格缩进 + 0x 窗口号
        if not re.match(r"^     0x[0-9a-fA-F]+ ", line):
            continue
        name = ""
        m = re.search(r'"([^"]*)"', line)
        if m:
            name = m.group(1)
        g = geo_re.search(line)
        if not g:
            continue
        w, h, x, y = (int(v) for v in g.groups())
        if name in ("__rec_region_selector__", "录屏工具"):
            continue
        if not name:
            continue   # 无名多为 override-redirect 辅助窗（输入法层等）
        # 未映射窗口的几何多为 (0,0)+小尺寸假值，排除（真全屏/近全屏除外）
        if x == 0 and y == 0 and w < 1900:
            continue
        # 屏内裁剪 + 尺寸过滤
        x2, y2 = min(x + w, 2160), min(y + h, 1440)
        x, y = max(0, x), max(0, y)
        w, h = x2 - x, y2 - y
        if w < 80 or h < 80:
            continue
        wins.append((x, y, w, h, name))
    wins.reverse()   # xwininfo 输出自底向上，逆序后最上层优先
    return wins


class RegionSelector(tk.Toplevel):
    """区域框选层（定格截图背景 + 窗口悬停识别）：
    · 鼠标悬浮：自动高亮识别到的应用窗口（绿框+名称）
    · 直接单击：选中该窗口作为录制区域
    · 按住拖动：自由框选（选区外暗化，红框+尺寸）
    · ESC：取消
    结果存入 self.region：逻辑坐标 (x, y, w, h)；取消为 None"""

    CLICK_SLOP = 8      # 按下/释放位移小于此值视为单击
    MIN_SIZE = 20

    def __init__(self, parent, bg_path=None, windows=None):
        super().__init__(parent)
        self.title("__rec_region_selector__")
        self.region = None
        self.windows = windows or []
        self.configure(bg="black")
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0,
                                cursor="cross")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 定格背景（截图失败则退化为暗屏+提示）
        self.bg_img = None
        if bg_path:
            try:
                self.bg_img = tk.PhotoImage(file=bg_path)
                self.canvas.create_image(0, 0, anchor="nw", image=self.bg_img)
            except Exception:
                self.bg_img = None
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        tip = "悬浮识别窗口，单击选中；按住拖动自由框选；ESC 取消"
        self.canvas.create_rectangle(sw // 2 - 330, 34, sw // 2 + 330, 72,
                                     fill="#000000", stipple="gray50", outline="")
        self.canvas.create_text(sw // 2, 53, text=tip, fill="white",
                                font=("Noto Sans CJK SC", 14))

        # 绘制对象 id 集合
        self._hover_items = []
        self._sel_items = []
        self.hover_win = None        # 当前悬停窗口 (x,y,w,h,name)
        self.press_hover = None      # 按下瞬间命中的窗口
        self.x0 = self.y0 = 0
        self._sw, self._sh = sw, sh

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda e: self.destroy())

    # ---- 工具：清除绘制 ----
    def _clear(self, items):
        for it in items:
            self.canvas.delete(it)
        items.clear()

    def _win_at(self, x, y):
        for w in self.windows:
            wx, wy, ww, wh, _ = w
            if wx <= x <= wx + ww and wy <= y <= wy + wh:
                return w
        return None

    # ---- 悬停识别 ----
    def _on_motion(self, e):
        w = self._win_at(e.x, e.y)
        if w is self.hover_win:
            return
        self.hover_win = w
        self._clear(self._hover_items)
        if w:
            x, y, ww, wh, name = w
            self._hover_items.append(self.canvas.create_rectangle(
                x, y, x + ww, y + wh, outline="#43d675", width=2))
            label = "{}  {}×{}".format(name or "窗口", ww, wh)
            ty = y - 12 if y >= 24 else y + 14
            self._hover_items.append(self.canvas.create_text(
                x + 4, ty, anchor="w", text=label, fill="#43d675",
                font=("Noto Sans CJK SC", 11, "bold")))

    # ---- 按下 ----
    def _on_press(self, e):
        self.x0, self.y0 = e.x, e.y
        self.press_hover = self._win_at(e.x, e.y)
        self._clear(self._sel_items)
        self._clear(self._hover_items)
        self.hover_win = None

    # ---- 拖动：自由框选（选区外 stipple 暗化） ----
    def _on_drag(self, e):
        if abs(e.x - self.x0) + abs(e.y - self.y0) < self.CLICK_SLOP:
            return   # 还在单击判定范围内，不画框
        self._draw_free(e.x, e.y)

    def _draw_free(self, cx, cy):
        self._clear(self._sel_items)
        x, y = min(self.x0, cx), min(self.y0, cy)
        w, h = abs(cx - self.x0), abs(cy - self.y0)
        SW, SH = self._sw, self._sh
        # 选区外四边暗化（模拟主流截图工具效果）
        dim = dict(fill="black", stipple="gray50", outline="")
        self._sel_items.append(self.canvas.create_rectangle(0, 0, SW, y, **dim))
        self._sel_items.append(self.canvas.create_rectangle(0, y + h, SW, SH, **dim))
        self._sel_items.append(self.canvas.create_rectangle(0, y, x, y + h, **dim))
        self._sel_items.append(self.canvas.create_rectangle(x + w, y, SW, y + h, **dim))
        # 选区边框 + 尺寸
        self._sel_items.append(self.canvas.create_rectangle(
            x, y, x + w, y + h, outline="#e53935", width=2))
        ty = y - 12 if y >= 24 else y + 14
        self._sel_items.append(self.canvas.create_text(
            x + 4, ty, anchor="w", text="{} × {}".format(w, h),
            fill="#ffeb3b", font=("Noto Sans CJK SC", 12, "bold")))

    # ---- 释放：单击选窗口 or 拖框区域 ----
    def _on_release(self, e):
        moved = abs(e.x - self.x0) + abs(e.y - self.y0)
        if moved < self.CLICK_SLOP and self.press_hover:
            x, y, w, h, _ = self.press_hover
            self.region = (x, y, w, h)
            self.destroy()
            return
        if moved >= self.CLICK_SLOP:
            x = min(self.x0, e.x)
            y = min(self.y0, e.y)
            w = abs(e.x - self.x0)
            h = abs(e.y - self.y0)
            if w >= self.MIN_SIZE and h >= self.MIN_SIZE:
                self.region = (x, y, w, h)
        self.destroy()


# ---------------- GUI 主窗口 ----------------
class RecorderApp(tk.Tk):
    BG = "#2b2b2b"
    FG = "#eeeeee"
    RED = "#e53935"
    YELLOW = "#fdd835"
    GRAY = "#9e9e9e"

    def __init__(self):
        super().__init__()
        self.title("录屏工具")
        self.configure(bg=self.BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)

        self.state = "idle"   # idle/countdown/recording/paused/merging
        self.engine = None
        self.worker_queue = queue.Queue()
        self._blink_on = True
        self._last_disk_check = 0.0
        self._countdown_left = 0
        self.last_output = ""
        self.done_panel = None      # 内嵌收尾面板（不再弹独立窗口）
        self.done_file = None
        self.cur_region = None      # 本次录制的物理区域 (x,y,w,h)；None=全屏

        cfg = load_config()
        self.output_dir = cfg.get("output_dir") or DEFAULT_OUT_DIR
        self.quality_name = cfg.get("quality") or "高清"

        self._build_ui()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        bar = tk.Frame(self, bg=self.BG, padx=10, pady=8)
        bar.pack()

        self.dot = tk.Label(bar, text="●", fg=self.GRAY, bg=self.BG,
                            font=("Noto Sans CJK SC", 14))
        self.dot.pack(side=tk.LEFT, padx=(0, 4))

        self.time_lbl = tk.Label(bar, text="00:00:00", fg=self.FG, bg=self.BG,
                                 font=("Noto Sans Mono", 13, "bold"))
        self.time_lbl.pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(bar, text="声音:", fg=self.GRAY, bg=self.BG,
                 font=("Noto Sans CJK SC", 10)).pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=AUDIO_MODES["system"])
        self.mode_box = ttk.Combobox(bar, textvariable=self.mode_var, width=9,
                                     values=list(AUDIO_MODES.values()), state="readonly",
                                     font=("Noto Sans CJK SC", 10))
        self.mode_box.pack(side=tk.LEFT, padx=(2, 6))

        tk.Label(bar, text="画质:", fg=self.GRAY, bg=self.BG,
                 font=("Noto Sans CJK SC", 10)).pack(side=tk.LEFT)
        self.quality_var = tk.StringVar(value=self.quality_name)
        self.quality_box = ttk.Combobox(bar, textvariable=self.quality_var, width=5,
                                        values=list(QUALITY.keys()), state="readonly",
                                        font=("Noto Sans CJK SC", 10))
        self.quality_box.pack(side=tk.LEFT, padx=(2, 6))

        tk.Label(bar, text="范围:", fg=self.GRAY, bg=self.BG,
                 font=("Noto Sans CJK SC", 10)).pack(side=tk.LEFT)
        self.scope_var = tk.StringVar(value="全屏")
        self.scope_box = ttk.Combobox(bar, textvariable=self.scope_var, width=4,
                                      values=["全屏", "区域"], state="readonly",
                                      font=("Noto Sans CJK SC", 10))
        self.scope_box.pack(side=tk.LEFT, padx=(2, 8))

        self.btn_start = self._mk_btn(bar, "▶ 开始", self.on_start, "#43a047")
        self.btn_pause = self._mk_btn(bar, "⏸ 暂停", self.on_pause, "#fb8c00")
        self.btn_stop = self._mk_btn(bar, "■ 停止", self.on_stop, self.RED)
        self.btn_close = self._mk_btn(bar, "✕", self.on_close, "#616161", width=3)
        self.btn_start.pack(side=tk.LEFT, padx=2)
        self.btn_pause.pack(side=tk.LEFT, padx=2)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        self.btn_close.pack(side=tk.LEFT, padx=(8, 0))

        # 麦克风设备行（B 方案：枚举真实输入源供选择，蓝牙设备标注）
        row_mic = tk.Frame(self, bg=self.BG, padx=10)
        row_mic.pack(fill=tk.X, pady=(2, 0))
        tk.Label(row_mic, text="麦克风:", fg=self.GRAY, bg=self.BG,
                 font=("Noto Sans CJK SC", 9)).pack(side=tk.LEFT)
        self.mic_map = {}            # 显示名 -> 真实 source 名
        self.mic_var = tk.StringVar(value="跟随系统默认")
        self.mic_box = ttk.Combobox(row_mic, textvariable=self.mic_var,
                                    width=26, state="readonly",
                                    font=("Noto Sans CJK SC", 9))
        self.mic_box.pack(side=tk.LEFT, padx=(2, 0))
        self.mic_box.bind("<<ComboboxSelected>>", self.on_mic_change)
        self.refresh_mic_devices()

        row2 = tk.Frame(self, bg=self.BG, padx=10)
        row2.pack(fill=tk.X)
        tk.Label(row2, text="输出:", fg=self.GRAY, bg=self.BG,
                 font=("Noto Sans CJK SC", 9)).pack(side=tk.LEFT)
        self.dir_lbl = tk.Label(row2, text=self._short_dir(), fg=self.FG, bg=self.BG,
                                font=("Noto Sans CJK SC", 9))
        self.dir_lbl.pack(side=tk.LEFT)
        self.btn_dir = tk.Button(row2, text="更改", command=self.on_change_dir,
                                 bg="#424242", fg=self.FG, relief=tk.FLAT,
                                 font=("Noto Sans CJK SC", 9), cursor="hand2", bd=0)
        self.btn_dir.pack(side=tk.RIGHT)

        self.status_lbl = tk.Label(self, text="就绪：选择声音与画质后点击「开始」",
                                   fg=self.GRAY, bg=self.BG, font=("Noto Sans CJK SC", 9),
                                   anchor="w", padx=10)
        self.status_lbl.pack(fill=tk.X, pady=(2, 6))
        self._refresh_buttons()

    def _short_dir(self, maxlen=38):
        d = self.output_dir
        return d if len(d) <= maxlen else "…" + d[-maxlen:]

    # ---- 麦克风设备选择（B 方案） ----
    def refresh_mic_devices(self):
        """枚举真实输入源刷新下拉；蓝牙设备标注。回到 idle 时也会调用"""
        self.mic_map = {"跟随系统默认": None}
        for name, desc in enum_input_sources():
            label = ("蓝牙｜" if "bluez" in name.lower() else "") + desc
            self.mic_map[label] = name
        self.mic_box.config(values=list(self.mic_map.keys()))
        if self.mic_var.get() not in self.mic_map:
            self.mic_var.set("跟随系统默认")
        log.info("麦克风设备列表已刷新: %s", list(self.mic_map.keys()))

    def on_mic_change(self, _e=None):
        name = self.mic_map.get(self.mic_var.get())
        if name and "bluez" in name.lower():
            self.status_lbl.config(
                text="提示：蓝牙耳机录音为电话音质（HFP 协议限制），追求音质请选板载麦克风")

    def _selected_mic(self):
        return self.mic_map.get(self.mic_var.get())

    def _mk_btn(self, parent, text, cmd, color, width=7):
        return tk.Button(parent, text=text, command=cmd, width=width,
                         bg=color, fg="white", activebackground=color,
                         relief=tk.FLAT, font=("Noto Sans CJK SC", 10),
                         cursor="hand2", bd=0, padx=4)

    def _mode_key(self):
        for k, v in AUDIO_MODES.items():
            if v == self.mode_var.get():
                return k
        return "system"

    def _crf(self):
        return QUALITY.get(self.quality_var.get(), 23)

    def _refresh_buttons(self):
        st = self.state
        self.btn_start.config(state=tk.NORMAL if st == "idle" else tk.DISABLED)
        self.btn_pause.config(state=tk.NORMAL if st in ("recording", "paused") else tk.DISABLED,
                              text="▶ 继续" if st == "paused" else "⏸ 暂停")
        self.btn_stop.config(state=tk.NORMAL if st in ("recording", "paused") else tk.DISABLED)
        ro = "readonly" if st == "idle" else tk.DISABLED
        self.mode_box.config(state=ro)
        self.quality_box.config(state=ro)
        self.scope_box.config(state=ro)
        self.mic_box.config(state=ro)
        self.btn_dir.config(state=tk.NORMAL if st == "idle" else tk.DISABLED)
        self.btn_close.config(state=tk.NORMAL if st not in ("merging", "countdown", "starting") else tk.DISABLED)

    # ---- 输出目录 ----
    def on_change_dir(self):
        d = filedialog.askdirectory(initialdir=self.output_dir, parent=self,
                                    title="选择录像保存目录")
        if d:
            self.output_dir = d
            self.dir_lbl.config(text=self._short_dir())
            cfg = load_config()
            cfg["output_dir"] = d
            cfg["quality"] = self.quality_var.get()
            save_config(cfg)
            self.status_lbl.config(text="输出目录已更改")

    # ---- 区域选择 ----
    def pick_region(self, pw):
        """弹框选层（定格截图背景+窗口悬停识别），返回逻辑坐标或 None"""
        self.withdraw()           # 先隐藏主窗，避免挡住框选
        bg = capture_background(pw)
        wins = enum_windows()
        sel = RegionSelector(self, bg, wins)
        sel.grab_set()
        self.wait_window(sel)
        self.deiconify()
        if bg:
            try:
                os.remove(bg)
            except OSError:
                pass
        return sel.region

    def _to_physical(self, region):
        """逻辑坐标 → 物理像素坐标（按 DRM 物理分辨率与逻辑分辨率比值换算，
        并做偶数对齐与边界裁剪）"""
        x, y, w, h = region
        phys = physical_screen_size()
        lw, lh = self.winfo_screenwidth(), self.winfo_screenheight()
        if phys and lw and lh:
            sx, sy = phys[0] / lw, phys[1] / lh
        else:
            sx = sy = 1.0
            phys = (lw, lh)
        X, Y = int(x * sx), int(y * sy)
        W, H = int(w * sx), int(h * sy)
        X -= X % 2
        Y -= Y % 2
        W = min(W - W % 2, phys[0] - X)
        H = min(H - H % 2, phys[1] - Y)
        W -= W % 2
        H -= H % 2
        if W < 32 or H < 32:
            messagebox.showwarning("区域太小",
                                   "选定区域过小（物理像素 {}×{}），请框大一点。".format(W, H),
                                   parent=self)
            return None
        return (X, Y, W, H)

    # ---- 开始（含 3 秒倒计时）----
    def on_start(self):
        pw = get_password(self)
        if not pw:
            return
        # 区域模式：先框选（每次开始都重新框选，全屏模式直接用全屏）
        self.cur_region = None
        if self.scope_var.get() == "区域":
            logic = self.pick_region(pw)
            if not logic:
                self.status_lbl.config(text="已取消区域选择")
                return
            self.cur_region = self._to_physical(logic)
            if not self.cur_region:
                return
            rx, ry, rw, rh = self.cur_region
            self.status_lbl.config(text="区域已选: {}×{} @ ({},{})".format(rw, rh, rx, ry))
        os.makedirs(self.output_dir, exist_ok=True)
        free = shutil.disk_usage(self.output_dir).free
        if free < DISK_GUARD_BYTES:
            messagebox.showwarning("磁盘空间不足",
                                   "输出目录剩余空间不足 1GB，请清理后再录。", parent=self)
            return
        self._hide_done_panel()     # 收起上次的收尾面板
        self.engine = Engine(pw)
        log.info("开始录制请求: 声音=%s 画质=%s 范围=%s 麦克风=%s 输出目录=%s 磁盘剩余=%.1fGB",
                 self._mode_key(), self.quality_var.get(), self.scope_var.get(),
                 self.mic_var.get(), self.output_dir, free / 1024 ** 3)
        cfg = load_config()
        cfg["quality"] = self.quality_var.get()
        save_config(cfg)
        # F1改进: 先启动录制进程并等首帧就绪，就绪后再走 3-2-1 倒计时，
        # 保证录像里能完整看到 3、2、1（此前倒计时3的那1秒进程还在启动，
        # 录像里只能看到从2开始）
        self._ffmpeg_ready = False
        self.state = "starting"
        self.status_lbl.config(text="准备中…")
        self._refresh_buttons()
        threading.Thread(target=self._start_worker,
                         args=(self._mode_key(), self._crf(), self.cur_region,
                               self._selected_mic()),
                         daemon=True).start()

    def _start_worker(self, mode, crf, region, mic_device=None):
        ok = self.engine.start(mode, crf, region, mic_device)
        self.worker_queue.put(("started", ok))

    def _do_countdown(self):
        if self._countdown_left > 0:
            self.time_lbl.config(text="  {}  ".format(self._countdown_left), fg=self.YELLOW)
            self.status_lbl.config(text="即将开始录制…")
            self._countdown_left -= 1
            self.after(1000, self._do_countdown)
        else:
            # 倒计时结束（进程早已就绪），直接进入录制态
            self.time_lbl.config(text="00:00:00", fg=self.FG)
            self._enter_recording()

    def _enter_recording(self):
        """进入录制态（F1：倒计时结束且进程已就绪时调用）"""
        # 议题三A：界面计时从倒计时结束清零起算——记录首段倒计时偏移（仅首次）
        if self.engine and self.engine.time_offset == 0.0 \
                and self.engine.seg_start_mono is not None:
            self.engine.time_offset = time.monotonic() - self.engine.seg_start_mono
            log.info("计时偏移已设定: %.2fs（倒计时阶段不计入界面计时）",
                     self.engine.time_offset)
        self.state = "recording"
        log.info("进入录制态")
        self._last_disk_check = time.monotonic()
        self.status_lbl.config(text="录制中（窗口保持显示，可手动最小化；工具条会被录入请注意）")
        notify("录屏已开始", "正在录制，点「停止」结束")
        self._refresh_buttons()

    # ---- 暂停/继续 ----
    def on_pause(self):
        if self.state == "recording":
            self.state = "merging"
            self._refresh_buttons()
            self.status_lbl.config(text="正在暂停…")
            threading.Thread(target=self._pause_worker, daemon=True).start()
        elif self.state == "paused":
            self.state = "merging"
            self._refresh_buttons()
            self.status_lbl.config(text="正在继续录制…")
            threading.Thread(target=self._resume_worker, daemon=True).start()

    def _pause_worker(self):
        self.engine.pause()
        self.worker_queue.put(("paused", True))

    def _resume_worker(self):
        ok = self.engine.resume(self._mode_key(), self._crf(), self.cur_region,
                                self._selected_mic())
        self.worker_queue.put(("resumed", ok))

    # ---- 停止 ----
    def on_stop(self, auto=False):
        log.info("停止录制%s", "（磁盘守护自动触发）" if auto else "（手动）")
        self.state = "merging"
        self._refresh_buttons()
        self.status_lbl.config(text="正在合成视频，请稍候…")
        self.dot.config(fg=self.YELLOW)
        if auto:
            notify("磁盘空间不足", "已自动安全停止并合成")
        out_name = timestamp_name() + ".mp4"
        out_path = os.path.join(self.output_dir, out_name)
        threading.Thread(target=self._stop_worker, args=(out_path,), daemon=True).start()

    def _stop_worker(self, out_path):
        out = self.engine.merge(out_path)
        self.worker_queue.put(("stopped", out))

    # ---- 定时刷新 ----
    def _tick(self):
        try:
            while True:
                tag, val = self.worker_queue.get_nowait()
                self._on_worker_done(tag, val)
        except queue.Empty:
            pass

        if self.state == "recording":
            self.time_lbl.config(text=fmt_time(self.engine.elapsed()))
            self._blink_on = not self._blink_on
            self.dot.config(fg=self.RED if self._blink_on else "#7f1d1d")
        elif self.state in ("countdown", "starting"):
            # 议题三D：预热阶段黄点闪烁（ffmpeg 已就绪，画面已在录制）
            self._blink_on = not self._blink_on
            self.dot.config(fg=self.YELLOW if self._blink_on else "#7f6a1d")
            # 磁盘守护
            now = time.monotonic()
            if now - self._last_disk_check > DISK_CHECK_INTERVAL:
                self._last_disk_check = now
                try:
                    if shutil.disk_usage(self.output_dir).free < DISK_GUARD_BYTES:
                        self.status_lbl.config(text="磁盘不足 1GB，自动停止…")
                        self.on_stop(auto=True)
                except Exception:
                    pass
        elif self.state == "paused":
            self.time_lbl.config(text=fmt_time(self.engine.elapsed()))
            self.dot.config(fg=self.YELLOW)
        self.after(400, self._tick)

    def _on_worker_done(self, tag, val):
        if tag == "started":
            if val:
                # 进程已就绪：从 3 开始走倒计时（此时录像已能录到数字）
                self._ffmpeg_ready = True
                self.state = "countdown"
                self._countdown_left = 3
                self._refresh_buttons()
                self._do_countdown()
            else:
                self.state = "idle"
                log.error("启动失败: %s", self.engine.last_error or "未知")
                self.status_lbl.config(text="启动失败: " + (self.engine.last_error or "未知"))
                messagebox.showerror("启动失败", self.engine.last_error or "未知错误", parent=self)
        elif tag == "paused":
            self.state = "paused"
            self.status_lbl.config(text="已暂停（点「继续」接着录，或点「停止」合成）")
        elif tag == "resumed":
            if val:
                self.state = "recording"
                self.status_lbl.config(text="录制中（窗口保持显示，可手动最小化）")
            else:
                self.state = "paused"
                messagebox.showerror("继续失败", self.engine.last_error or "未知错误", parent=self)
        elif tag == "stopped":
            self.state = "idle"
            self.time_lbl.config(text="00:00:00")
            self.dot.config(fg=self.GRAY)
            self.refresh_mic_devices()      # 蓝牙等设备可能插拔，回到 idle 刷新列表
            self.deiconify()            # 若用户手动最小化了，唤回展示收尾面板
            self.lift()
            if val:
                self.last_output = val
                size_mb = os.path.getsize(val) / 1024 / 1024
                self.status_lbl.config(text="已保存: {}（{:.1f} MB）".format(
                    os.path.basename(val), size_mb))
                notify("录屏已保存", "{}（{:.1f} MB）".format(os.path.basename(val), size_mb))
                self._show_done_panel(val)      # 内嵌面板，不弹新窗口
                if self.engine and self.engine.warning:
                    self.done_hint.config(text=self.engine.warning)
                    self.engine.warning = ""
            else:
                log.error("合成失败: %s", self.engine.last_error or "未知")
                self.status_lbl.config(text="合成失败: " + (self.engine.last_error or ""))
                messagebox.showerror("合成失败", self.engine.last_error or "未知错误", parent=self)
        self._refresh_buttons()

    # ---- 内嵌收尾面板（重命名/另存/打开/播放） ----
    def _show_done_panel(self, file_path):
        self._hide_done_panel()
        self.done_file = file_path
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        panel = tk.Frame(self, bg="#383838", padx=8, pady=6)
        panel.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.done_panel = panel

        tk.Label(panel, text="已保存: {}（{:.1f} MB）".format(
            os.path.basename(file_path), size_mb),
            fg=self.FG, bg="#383838", font=("Noto Sans CJK SC", 9, "bold"),
            anchor="w").pack(fill=tk.X)

        row1 = tk.Frame(panel, bg="#383838")
        row1.pack(fill=tk.X, pady=(4, 2))
        tk.Label(row1, text="文件名:", fg=self.GRAY, bg="#383838",
                 font=("Noto Sans CJK SC", 9)).pack(side=tk.LEFT)
        self.done_name_var = tk.StringVar(
            value=os.path.splitext(os.path.basename(file_path))[0])
        entry = tk.Entry(row1, textvariable=self.done_name_var, width=20,
                         font=("Noto Sans CJK SC", 9))
        entry.pack(side=tk.LEFT, padx=4)
        entry.select_range(0, tk.END)
        self._panel_btn(row1, "✔ 重命名", self._done_rename, "#43a047", 8).pack(side=tk.LEFT)

        row2 = tk.Frame(panel, bg="#383838")
        row2.pack(fill=tk.X, pady=2)
        self._panel_btn(row2, "💾 另存到…", self._done_save_as, "#8e24aa").pack(side=tk.LEFT, padx=(0, 4))
        self._panel_btn(row2, "📁 打开文件夹", self._done_open_dir, "#1e88e5").pack(side=tk.LEFT, padx=(0, 4))
        self._panel_btn(row2, "▶ 播放", self._done_play, "#fb8c00", 7).pack(side=tk.LEFT, padx=(0, 4))
        self._panel_btn(row2, "收起", self._hide_done_panel, "#616161", 6).pack(side=tk.RIGHT)

        self.done_hint = tk.Label(panel, text="", fg=self.YELLOW, bg="#383838",
                                  font=("Noto Sans CJK SC", 9), anchor="w")
        self.done_hint.pack(fill=tk.X)
        # 关键：部分窗口管理器下 topmost 窗口 pack 后不自适应高度，
        # 强制重算，确保面板可见
        self.update_idletasks()
        self.geometry("")

    def _hide_done_panel(self):
        if self.done_panel:
            self.done_panel.destroy()
            self.done_panel = None
            self.done_file = None

    def _panel_btn(self, parent, text, cmd, color, width=10):
        return tk.Button(parent, text=text, command=cmd, width=width,
                         bg=color, fg="white", activebackground=color,
                         relief=tk.FLAT, font=("Noto Sans CJK SC", 9),
                         cursor="hand2", bd=0)

    def _done_rename(self):
        new = self.done_name_var.get().strip()
        if not new:
            self.done_hint.config(text="文件名不能为空")
            return
        if any(c in BAD_CHARS for c in new):
            self.done_hint.config(text='文件名不能包含  / \\ : * ? " < > |')
            return
        dir_name = os.path.dirname(self.done_file)
        target = os.path.join(dir_name, new + ".mp4")
        if target == self.done_file:
            self.done_hint.config(text="文件名未变化")
            return
        if os.path.exists(target):
            base, i = target, 1
            while os.path.exists(base):
                base = os.path.join(dir_name, "{}_{}.mp4".format(new, i))
                i += 1
            target = base
        try:
            os.rename(self.done_file, target)
            self.done_file = target
            self.last_output = target
            self.done_hint.config(text="已重命名为: " + os.path.basename(target))
        except Exception as e:
            self.done_hint.config(text="重命名失败: " + str(e))

    def _done_save_as(self):
        target = filedialog.asksaveasfilename(
            parent=self, title="另存到",
            initialdir=os.path.dirname(self.done_file),
            initialfile=os.path.basename(self.done_file),
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4")])
        if not target:
            return
        if os.path.exists(target):
            if not messagebox.askyesno("文件已存在", "目标文件已存在，覆盖吗？", parent=self):
                return
        try:
            shutil.move(self.done_file, target)
            self.done_file = target
            self.last_output = target
            self.done_name_var.set(os.path.splitext(os.path.basename(target))[0])
            self.done_hint.config(text="已另存到: " + target)
        except Exception as e:
            self.done_hint.config(text="另存失败: " + str(e))

    def _done_open_dir(self):
        subprocess.Popen(["xdg-open", os.path.dirname(self.done_file)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _done_play(self):
        subprocess.Popen(["xdg-open", self.done_file],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def on_close(self):
        if self.state in ("countdown", "starting"):
            # 启动过渡期：忽略关闭，防止录制进程成为孤儿
            return
        if self.state in ("recording", "paused"):
            if not messagebox.askyesno("正在录制", "正在录制中，要停止并保存吗？", parent=self):
                return
            self.on_stop()
            self.after(500, self._wait_merge_then_close)
            return
        self.destroy()

    def _wait_merge_then_close(self):
        if self.state == "merging":
            self.after(500, self._wait_merge_then_close)
        else:
            self.destroy()


if __name__ == "__main__":
    setup_logging()
    log_session_boot()
    try:
        app = RecorderApp()
        app.mainloop()
    except Exception:
        log.exception("主界面异常退出")
        raise
    log.info("会话结束")
