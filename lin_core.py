#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LIN LDF 解析与代码生成核心模块（无 GUI 依赖）。

本模块提供:
  - 通用 LIN 2.x LDF 解析(节点/信号/帧/诊断帧/零星帧/调度表)
  - 数据模型(Frame / Signal / Schedule / SporadicGroup / Model)
  - ROM database 数组生成(严格遵循约定的分段布局)
  - LIN1_BCM_lin1_hand.h / LIN1_BCM_lin1_dbc.c / Master_lin1_ldf.h 生成
"""

import os
import re
from datetime import datetime


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class Signal:
    """一条帧内的信号。"""

    def __init__(self, name='', length=1, start=0, init_value=0):
        self.name = name          # 信号名
        self.length = int(length)  # bit 大小 (SignalBitSize)
        self.start = int(start)    # 帧内起始 bit 偏移 (BitOffset)
        self.init_value = int(init_value)  # LDF 中信号的初始值

    def copy(self):
        return Signal(self.name, self.length, self.start, self.init_value)


class Frame:
    """一个 LIN 帧(ID)。"""

    def __init__(self, name='', frame_id=0, publisher='', subscribers=None, size=8):
        self.name = name
        self.frame_id = int(frame_id) & 0x3F   # 6-bit LIN Frame ID
        self.publisher = publisher             # 发送方
        self.subscribers = list(subscribers or [])  # 接收方
        self.size = int(size)                  # 字节长度
        self.signals = []                       # list[Signal]
        self.init_bytes = [0] * self.size       # 每字节初始值, 默认 0x00

    def copy(self):
        f = Frame(self.name, self.frame_id, self.publisher, list(self.subscribers), self.size)
        f.signals = [s.copy() for s in self.signals]
        f.init_bytes = list(self.init_bytes[:self.size]) + [0] * (self.size - len(self.init_bytes)) if len(self.init_bytes) >= self.size else list(self.init_bytes) + [0] * (self.size - len(self.init_bytes))
        return f

    def resize(self, new_size):
        """调整帧长度, 初始值字节随长度增减(默认补 0)。"""
        new_size = int(new_size)
        if new_size <= 0:
            new_size = 1
        old = self.init_bytes
        self.init_bytes = [old[i] if i < len(old) else 0 for i in range(new_size)]
        self.size = new_size

    def tx(self, master):
        """是否为发送帧(主节点发布)。"""
        return self.publisher == master

    def direction_str(self, master):
        return 'TX' if self.tx(master) else 'RX'


class ScheduleEntry:
    """调度表条目: 帧名 + tick 数。"""

    def __init__(self, frame_name='', tick=1):
        self.frame_name = frame_name
        self.tick = int(tick)

    def copy(self):
        return ScheduleEntry(self.frame_name, self.tick)


class Schedule:
    """一个调度表。"""

    def __init__(self, name=''):
        self.name = name
        self.entries = []  # list[ScheduleEntry]

    def copy(self):
        s = Schedule(self.name)
        s.entries = [e.copy() for e in self.entries]
        return s


class SporadicGroup:
    """一个零星帧组(多个帧共享一个时隙)。"""

    def __init__(self, name=''):
        self.name = name
        self.frame_names = []  # list[str]

    def copy(self):
        g = SporadicGroup(self.name)
        g.frame_names = list(self.frame_names)
        return g


class Model:
    """解析后的完整 LDF 模型。"""

    def __init__(self):
        self.master = 'BCM'
        self.slaves = []
        self.frames = []       # list[Frame] (保持 LDF 声明顺序 => FrameIndex)
        self.schedules = []    # list[Schedule]
        self.sporadic = []     # list[SporadicGroup]
        self.network = 'LIN1'
        self.source_path = ''

    def nodes(self):
        return [self.master] + list(self.slaves)

    def frame_index(self, name):
        for i, f in enumerate(self.frames):
            if f.name == name:
                return i
        return None

    def sporadic_frame_names(self):
        names = set()
        for g in self.sporadic:
            names.update(g.frame_names)
        return names


# ---------------------------------------------------------------------------
# LDF 解析辅助
# ---------------------------------------------------------------------------
def _strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'//[^\n]*', '', text)
    return text


def _block(text, name):
    m = re.search(r'(?:^|\n)\s*' + re.escape(name) + r'\s*\{', text, flags=re.M)
    if not m:
        return ''
    start = m.end() - 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return ''


def _num(value):
    value = value.strip()
    return int(value, 16) if value.lower().startswith('0x') else int(value)


# ---------------------------------------------------------------------------
# LDF 解析
# ---------------------------------------------------------------------------
def parse_ldf(path):
    """解析 LDF 文件, 返回 Model。解析失败抛 ValueError。"""
    if not os.path.isfile(path):
        raise ValueError(f'LDF 文件不存在: {path}')
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        text = _strip_comments(f.read())

    model = Model()
    model.source_path = path
    model.network = detect_lin_network(path)

    # Nodes
    nodes = _block(text, 'Nodes')
    mm = re.search(r'Master\s*:\s*(\w+)', nodes)
    if mm:
        model.master = mm.group(1)
    sm = re.search(r'Slaves?\s*:\s*(.*?)\s*;', nodes, flags=re.S)
    if sm:
        for n in sm.group(1).split(','):
            n = n.strip()
            if n:
                model.slaves.append(n)

    # Signals / Diagnostic_signals: name -> (length, init_value)
    sig_length = {}
    sig_init = {}
    for seg in ('Signals', 'Diagnostic_signals'):
        body = _block(text, seg)
        for item in body.split(';'):
            item = item.strip()
            if not item:
                continue
            m = re.match(r'([A-Za-z_][A-Za-z0-9_/]*)\s*:\s*(\d+)\s*,\s*(0x[0-9A-Fa-f]+|\d+)', item)
            if m:
                sig_length[m.group(1)] = int(m.group(2))
                sig_init[m.group(1)] = _num(m.group(3))

    # Frames (普通帧)
    _parse_frames(_block(text, 'Frames'), model, sig_length, sig_init, diagnostic=False)
    # Diagnostic_frames (诊断帧, 无 publisher/size)
    _parse_frames(_block(text, 'Diagnostic_frames'), model, sig_length, sig_init, diagnostic=True)

    # Sporadic_frames (零星帧)
    _parse_sporadic(_block(text, 'Sporadic_frames'), model)

    # Schedule_tables
    _parse_schedules(_block(text, 'Schedule_tables'), model)

    return model


def _parse_frames(body, model, sig_length, sig_init, diagnostic):
    if diagnostic:
        pat = re.compile(r'(\w+)\s*:\s*(0x[0-9A-Fa-f]+|\d+)\s*\{', re.S)
    else:
        pat = re.compile(r'(\w+)\s*:\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*(\w+)\s*,\s*(\d+)\s*,?\s*\{', re.S)
    pos = 0
    while True:
        m = pat.search(body, pos)
        if not m:
            break
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(body)):
            if body[i] == '{':
                depth += 1
            elif body[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        fname = m.group(1)
        fid = _num(m.group(2))
        if diagnostic:
            pub = model.master if fid == 0x3C else (model.slaves[0] if model.slaves else 'RLS')
            size = 8
        else:
            pub = m.group(3)
            size = int(m.group(4))

        frame = Frame(fname, fid, pub, _default_subscribers(model, pub), size)
        for line in body[start + 1:end].split(';'):
            sm2 = re.match(r'\s*([A-Za-z_][A-Za-z0-9_/]*)\s*,\s*(\d+)\s*$', line.strip())
            if sm2:
                sname = sm2.group(1)
                slen = sig_length.get(sname, 8 if sname.startswith(('MasterReq', 'SlaveResp')) else 1)
                sval = sig_init.get(sname, 0)
                frame.signals.append(Signal(sname, slen, int(sm2.group(2)), sval))
        rebuild_init_bytes(frame)
        model.frames.append(frame)
        pos = end + 1


def rebuild_init_bytes(frame):
    """按各信号 bit 布局(起始 bit 为 LSB)将信号初始值合并成帧的每字节初始值。

    未被子信号覆盖的 bit 保持为 0。
    """
    data = [0] * frame.size
    for sig in frame.signals:
        value = int(getattr(sig, 'init_value', 0) or 0)
        if not value:
            continue
        for bit in range(sig.length):
            if (value >> bit) & 1:
                pos = sig.start + bit
                if pos < frame.size * 8:
                    data[pos // 8] |= 1 << (pos % 8)
    frame.init_bytes = data
    return data


def read_signal_init_values(frame):
    """由帧字节初始值反推每个信号的初始值, 写回 signal.init_value。"""
    for sig in frame.signals:
        value = 0
        for bit in range(sig.length):
            pos = sig.start + bit
            if pos < frame.size * 8 and (pos // 8) < len(frame.init_bytes):
                if (frame.init_bytes[pos // 8] >> (pos % 8)) & 1:
                    value |= 1 << bit
        sig.init_value = value


def _default_subscribers(model, publisher):
    return [n for n in model.nodes() if n != publisher]


def _parse_sporadic(body, model):
    for item in body.split(';'):
        item = item.strip()
        if not item:
            continue
        m = re.match(r'([A-Za-z_][A-Za-z0-9_/]*)\s*:\s*(.*)$', item)
        if not m:
            continue
        g = SporadicGroup(m.group(1))
        for n in m.group(2).split(','):
            n = n.strip()
            if n:
                g.frame_names.append(n)
        model.sporadic.append(g)


def _parse_schedules(body, model):
    pat = re.compile(r'(\w+)\s*\{', re.S)
    pos = 0
    while True:
        m = pat.search(body, pos)
        if not m:
            break
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(body)):
            if body[i] == '{':
                depth += 1
            elif body[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        sch = Schedule(m.group(1))
        for line in body[start + 1:end].split(';'):
            dm = re.match(r'\s*(\w+)\s+delay\s+(\d+)\s*ms?\s*$', line.strip())
            if dm:
                name = dm.group(1)
                tick = max(1, int(dm.group(2)) // 10)
                sch.entries.append(ScheduleEntry(name, tick))
        model.schedules.append(sch)
        pos = end + 1


def detect_lin_network(path):
    m = re.findall(r'LIN(\d+)', os.path.basename(path).upper())
    return 'LIN' + m[0] if m else 'LIN1'


# ---------------------------------------------------------------------------
# PID 计算 (LIN 2.x Protected ID)
# ---------------------------------------------------------------------------
def compute_pid(fid):
    fid &= 0x3F
    b = [(fid >> i) & 1 for i in range(6)]
    p0 = b[0] ^ b[1] ^ b[2] ^ b[4]
    p1 = 1 ^ (b[1] ^ b[3] ^ b[4] ^ b[5])
    return fid | (p0 << 6) | (p1 << 7)


# ---------------------------------------------------------------------------
# database / 地址计算 (遵循约定的分段布局, 零星帧忽略)
# ---------------------------------------------------------------------------
def schedule_entry_count(sch, sporadic_names):
    """调度表中非零星帧的条目数。"""
    return sum(1 for e in sch.entries if e.frame_name not in sporadic_names)


def schedule_total_tick(sch, sporadic_names):
    """调度表总 tick = 各条目 tick 之和 + 1。"""
    total = sum(e.tick for e in sch.entries if e.frame_name not in sporadic_names)
    return total + 1


def compute_addresses(model):
    """返回各地址宏对应的数组下标。n=调度表个数, m=总 ID 数。"""
    sporadic_names = model.sporadic_frame_names()
    n = len(model.schedules)
    m = len(model.frames)
    entries = sum(schedule_entry_count(s, sporadic_names) for s in model.schedules)

    sch_start = 0
    sch_len_start = entries * 2
    tick_max_start = sch_len_start + n
    init_start = tick_max_start + n * 2
    id_start = init_start + sum(f.size for f in model.frames)
    id_dir_start = id_start + m
    id_len_start = id_dir_start + m
    return {
        'sch_start': sch_start,
        'sch_len_start': sch_len_start,
        'tick_max_start': tick_max_start,
        'init_start': init_start,
        'sporadic_start': init_start,   # 零星帧忽略, 与初始值区共用起始
        'spor_len_start': init_start,
        'id_start': id_start,
        'id_dir_start': id_dir_start,
        'id_len_start': id_len_start,
    }


def build_database(model):
    """按约定布局生成 ROM database 字节列表。"""
    sporadic_names = model.sporadic_frame_names()
    data = []

    # 1) 前 n 行: 每个调度表一条目 {Frame_ID 索引, Tick_Count}
    for sch in model.schedules:
        for e in sch.entries:
            if e.frame_name in sporadic_names:
                continue
            idx = model.frame_index(e.frame_name)
            if idx is None:
                continue
            data.append(idx & 0xFF)
            data.append(e.tick & 0xFF)

    # 2) 第 n+1 行: 每个调度表包含的 ID 数
    for sch in model.schedules:
        data.append(schedule_entry_count(sch, sporadic_names) & 0xFF)

    # 3) 第 n+2 行: 每个调度表 {0x00, 总Tick}
    for sch in model.schedules:
        data.append(0x00)
        data.append(schedule_total_tick(sch, sporadic_names) & 0xFF)

    # 4) 第 n+3 ~ n+3+m 行: 每个 ID 的字节初始值
    for f in model.frames:
        for i in range(f.size):
            v = f.init_bytes[i] if i < len(f.init_bytes) else 0
            data.append(v & 0xFF)

    # 5) 第 n+m+4 行: 所有 ID 的 PID
    for f in model.frames:
        data.append(compute_pid(f.frame_id) & 0xFF)

    # 6) 第 n+m+5 行: 发送/接收标志 (发送 0x01, 接收 0x00)
    for f in model.frames:
        data.append(1 if f.tx(model.master) else 0)

    # 7) 第 n+m+6 行: 所有 ID 的字节长度
    for f in model.frames:
        data.append(f.size & 0xFF)

    return data


# ---------------------------------------------------------------------------
# 文件生成
# ---------------------------------------------------------------------------
def _net(network):
    up = network.upper()
    return up.lower(), up


def _macro_name(name):
    return re.sub(r'[^A-Za-z0-9_]', '_', name)


def _signal_type(sig):
    if sig.length == 1:
        return 'bool'
    if sig.length <= 8:
        return 'u8'
    return 'u16'


def _handle_value(sig, frame_index):
    return (sig.length << 16) | (sig.start << 8) | frame_index


def _reserved_skip_for_signal(sig):
    """大块 Reserved 信号(>16bit)不生成 signal 宏(与参考文件一致)。"""
    return sig.name.startswith('Reserved') and sig.length > 16


def _reserved_skip_for_flag(sig, direction):
    if sig.name.startswith('Reserved') and sig.length > 16:
        return not (direction == 'RX' and sig.length == 40)
    return False


def gen_hand(model):
    """生成 LIN1_BCM_lin1_hand.h 内容。"""
    lower, upper = _net(model.network)
    prefix = f'{upper}_BCM_{upper}'
    a = compute_addresses(model)
    n = len(model.schedules)
    m = len(model.frames)
    lines = [
        '/******************************************************************************',
        'COPYRIGHT 2026   : ATECH', 'Project          : LIN',
        f'Source File Name : {upper}_BCM_{lower}_hand.h', 'Group            : SoftWare Team',
        'Author           : LIN_CFG_Editor', f'Date First Issued: {datetime.now().strftime("%m/%d/%Y")}',
        '******************************************************************************/',
        f'#ifndef {upper}_BCM_{lower}_hand_h', f'#define {upper}_BCM_{lower}_hand_h', '',
        '/* #include */',
        f'#include "Master_{lower}_syst.h"', f'#include "Master_{lower}_htype.h"', '',
        '/* arrays */', f'extern l_const l_u8 {upper}_ROMDatabase[];', '',
        '/* #define */',
        f'#define {upper}_SCH_TABLE_SIZE    {n}',
        f'#define {upper}_ID_TABLE_SIZE     {m}',
        f'#define {upper}_SPOR_TABLE_SIZE   0', '',
        f'#define {upper}_SCH_START_ADDR       {a["sch_start"]}',
        f'#define {upper}_SCH_LEN_START_ADDR   {a["sch_len_start"]}',
        f'#define {upper}_TICK_MAX_START_ADDR  {a["tick_max_start"]}',
        f'#define {upper}_SPORADIC_START_ADDR  {a["sporadic_start"]}',
        f'#define {upper}_SPOR_LEN_START_ADDR  {a["spor_len_start"]}',
        f'#define {upper}_INIT_VAL_START_ADDR  {a["init_start"]}',
        f'#define {upper}_ID_START_ADDR        {a["id_start"]}',
        f'#define {upper}_ID_DIR_START_ADDR    {a["id_dir_start"]}',
        f'#define {upper}_ID_LEN_START_ADDR    {a["id_len_start"]}', '',
        f'#define {upper}_DATAREQUEST_MAX_LEN  {max((f.size for f in model.frames if f.tx(model.master)), default=8)}',
        f'#define {upper}_DATASEND_MAX_LEN     {max((f.size for f in model.frames if not f.tx(model.master)), default=8)}', '',
    ]
    for i, sch in enumerate(model.schedules):
        lines.append(f'#define {upper}_SCH{i}_NUM  {schedule_entry_count(sch, model.sporadic_frame_names())}')
    for i in range(n, 5):
        lines.append(f'#define {upper}_SCH{i}_NUM  1')
    lines += ['']
    for i in range(5):
        lines.append(f'#define {upper}_SPOR{i}_NUM  1')
    lines += ['']
    for i, f in enumerate(model.frames):
        lines.append(f'#define {upper}_ID{i}_Frame_LENTH  {f.size}')
    for i in range(m, 20):
        lines.append(f'#define {upper}_ID{i}_Frame_LENTH  1')
    lines += ['', '/* #define schedules */',
              f'#define {upper}_NULL_SCHEDULE   ((l_schedule_handle) 0xff)']
    for i in range(1, n + 1):
        lines.append(f'#define {upper}_SCHEDUEL{i}_l_schedule_Handle   ((l_schedule_handle) {i - 1})')

    # 信号宏
    all_sigs = []
    for fi, f in enumerate(model.frames):
        for sig in f.signals:
            if _reserved_skip_for_signal(sig):
                continue
            all_sigs.append((_signal_type(sig), f.direction_str(model.master), sig, fi))
    for typ in ('bool', 'u8', 'u16'):
        group = [x for x in all_sigs if x[0] == typ]
        if not group:
            continue
        lines += ['', '/* #define signals */']
        width = max(len(f'#define {prefix}_{d}_{_macro_name(s.name)}_l_signal_{typ}_Handle') for _, d, s, _ in group) + 4
        for _, direction, sig, fi in group:
            macro = f'#define {prefix}_{direction}_{_macro_name(sig.name)}_l_signal_{typ}_Handle'
            lines.append(f'{macro:<{width}} ((l_signal_{typ}_handle) 0x{_handle_value(sig, fi):X})')

    # 信号 flags 宏
    lines += ['', '/* #define signal flags */']
    flags = []
    for fi, f in enumerate(model.frames):
        direction = f.direction_str(model.master)
        for sig in f.signals:
            if _reserved_skip_for_flag(sig, direction):
                continue
            flags.append((f'#define {prefix}_{direction}_{_macro_name(sig.name)}_l_flag_Handle', fi))
    if flags:
        width = max(len(x[0]) for x in flags) + 4
        for macro, fi in flags:
            lines.append(f'{macro:<{width}} ((l_flag_handle) 0x{fi:X})')

    lines += ['', '#endif', '']
    return '\n'.join(lines)


def gen_dbc(model):
    """生成 LIN1_BCM_lin1_dbc.c 内容。"""
    lower, upper = _net(model.network)
    data = build_database(model)
    a = compute_addresses(model)

    # 按"行"分组
    groups = []
    pos = 0
    sporadic_names = model.sporadic_frame_names()
    for sch in model.schedules:
        nbytes = schedule_entry_count(sch, sporadic_names) * 2
        groups.append(data[pos:pos + nbytes])
        pos += nbytes
    groups.append(data[a['sch_len_start']:a['tick_max_start']])
    groups.append(data[a['tick_max_start']:a['init_start']])
    p = a['init_start']
    for f in model.frames:
        groups.append(data[p:p + f.size])
        p += f.size
    groups += [data[a['id_start']:a['id_dir_start']],
               data[a['id_dir_start']:a['id_len_start']],
               data[a['id_len_start']:]]

    lines = [
        '/******************************************************************************',
        'COPYRIGHT 2026   : ATECH', 'Project          : LIN',
        f'Source File Name : {upper}_BCM_{lower}_dbc.c', 'Group            : SoftWare Team',
        'Author           : LIN_CFG_Editor', f'Date First Issued: {datetime.now().strftime("%m/%d/%Y")}',
        '******************************************************************************/',
        f'#define {upper}_BCM_{lower}_dbc_c', '',
        '/* #include */', f'#include "Master_{lower}_syst.h"', '',
        '/* arrays */', f'l_const l_u8 {upper}_ROMDatabase[]=', '{',
    ]
    real = [g for g in groups if g]
    for i, g in enumerate(real):
        tail = ',' if i < len(real) - 1 else ''
        lines.append('\t' + ', '.join(f'0x{x:02X}' for x in g) + tail + ' ')
    lines += ['};', '', '/* #AtechLINSignature */', '']
    return '\n'.join(lines)


def gen_ldf_h(model):
    lower, upper = _net(model.network)
    today = datetime.now().strftime('%m/%d/%Y')
    return '\n'.join([
        '/******************************************************************************',
        'COPYRIGHT 2026   : ATECH', 'Project          : LIN',
        f'Source File Name : Master_{lower}_ldf.h', 'Group            : SoftWare Team',
        'Author           : LIN_CFG_Editor', f'Date First Issued: {today}',
        '******************************************************************************/',
        f'#ifndef Master_{lower}_ldf_h', f'#define Master_{lower}_ldf_h', '',
        '/* #include */', f'#include "{upper}_BCM_{lower}_hand.h"', '', '#endif', '',
    ])


def generate_files(model, output_dir):
    """生成三个文件, 返回 (成功, 消息, [文件路径])。"""
    lower, upper = _net(model.network)
    os.makedirs(output_dir, exist_ok=True)
    files = [
        (os.path.join(output_dir, f'{upper}_BCM_{lower}_hand.h'), gen_hand(model)),
        (os.path.join(output_dir, f'{upper}_BCM_{lower}_dbc.c'), gen_dbc(model)),
        (os.path.join(output_dir, f'Master_{lower}_ldf.h'), gen_ldf_h(model)),
    ]
    for path, content in files:
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(content.replace('\n', '\r\n'))
    return True, f'生成成功: {len(model.frames)} 个 ID, {len(model.schedules)} 个调度表, database {len(build_database(model))} 字节', [x[0] for x in files]
