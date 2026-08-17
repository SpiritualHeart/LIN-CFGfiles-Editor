#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LIN CFG Editor - 将 LDF 生成 LIN1_CFGs 三文件的交互式 GUI 工具。

功能:
  1. 导入 LDF, 展示 ID(帧) 信息: 名称 / LIN ID / 发送方->接收方 / 长度 / 信号
  2. 编辑每个 ID 的字节初始值(默认 0x00), 用于生成 dbc.c 的 ROM database
  3. 增删改 ID 与帧信号
  4. 识别并编辑调度表与零星帧
  5. 选择导出路径(默认=导入路径)并生成三个文件
"""

import os
import re
import sys

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QComboBox, QAbstractItemView,
)

try:
    from lin_core import (Model, Frame, Signal, Schedule, ScheduleEntry,
                          SporadicGroup, parse_ldf, generate_files, build_database,
                          compute_addresses, compute_pid, detect_lin_network,
                          rebuild_init_bytes, read_signal_init_values)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from lin_core import (Model, Frame, Signal, Schedule, ScheduleEntry,
                          SporadicGroup, parse_ldf, generate_files, build_database,
                          compute_addresses, compute_pid, detect_lin_network,
                          rebuild_init_bytes, read_signal_init_values)


# 软件版本号: 主.次.修订, 每位最大 99; 未指定新版本号时在末位自动 +1
APP_VERSION = 'V1.0.1'


# ID 表格列
F_COL_NAME = 0
F_COL_ID = 1
F_COL_PUB = 2
F_COL_SUB = 3
F_COL_SIZE = 4
F_COL_DIR = 5

# 信号表格列
S_COL_NAME = 0
S_COL_START = 1
S_COL_LEN = 2
S_COL_INIT = 3


def parse_hex(value):
    value = value.strip().lower()
    if value.startswith('0x'):
        return int(value, 16)
    return int(value, 16)


def parse_hex_bytes(text):
    """解析 "00 00 00 00" 形式的字节串, 返回 list[int]。失败抛 ValueError。"""
    parts = text.replace(',', ' ').split()
    out = []
    for p in parts:
        out.append(int(p, 16) & 0xFF)
    return out


def format_hex_bytes(data):
    return ' '.join('%02X' % x for x in data)


def format_subscribers(subs):
    return ', '.join(subs) if subs else ''


class DragDropTable(QTableWidget):
    """支持拖放调整行顺序的表格; drop 完成后发出 rowsDropped(src_row, dst_row)。

    不使用 QTableWidget 内置的 InternalMove(其移动行时会丢失单元格内容),
    而是在拖拽启动时锁定源行, drop 时仅发出源/目标行号,
    由外部负责更新数据并刷新表格。
    """

    rowsDropped = pyqtSignal(int, int)
    dragFinished = pyqtSignal()

    def __init__(self, rows=0, cols=0, parent=None):
        super().__init__(rows, cols, parent)
        self._drag_src_row = -1
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)

    def _lock_drag_source(self):
        """拖拽启动时锁定源行, 避免 drop 时 selection 已变化导致判错行。"""
        rows = sorted(idx.row() for idx in self.selectionModel().selectedRows())
        self._drag_src_row = rows[0] if rows else -1

    def _resolve_dst(self, pos):
        """根据松手位置计算目标行号(插入位置)。"""
        idx = self.indexAt(pos)
        if idx.isValid():
            dst = idx.row()
            # 落在行下半区则插到该行之后
            if pos.y() > self.visualRect(idx).center().y():
                dst += 1
        else:
            dst = self.rowCount()
        return dst

    def startDrag(self, supportedActions):
        self._lock_drag_source()
        super().startDrag(supportedActions)
        # Qt 在 MoveAction 拖放结束后会调用 clearOrRemove 清除源项内容,
        # 因此在这里(整个拖放流程彻底结束后)通知外部按 model 全量重建,
        # 避免名称列/条目数列文字被清空。
        self.dragFinished.emit()

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        if self._drag_src_row < 0:
            self._lock_drag_source()
        if self._drag_src_row < 0:
            event.ignore()
            return
        src = self._drag_src_row
        dst = self._resolve_dst(event.pos())
        event.setDropAction(Qt.MoveAction)
        event.accept()
        self._drag_src_row = -1
        self.rowsDropped.emit(src, dst)


class AddFrameDialog(QDialog):
    def __init__(self, master, slaves, parent=None):
        super().__init__(parent)
        self.setWindowTitle('添加 ID')
        self.master = master
        self.slaves = slaves
        layout = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.id_edit = QLineEdit('0x00')
        self.pub_edit = QComboBox()
        self.pub_edit.setEditable(True)
        for n in [master] + slaves:
            self.pub_edit.addItem(n)
        self.sub_edit = QLineEdit(', '.join(slaves))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 255)
        self.size_spin.setValue(8)
        layout.addRow('帧名称:', self.name_edit)
        layout.addRow('LIN ID (hex):', self.id_edit)
        layout.addRow('发送方:', self.pub_edit)
        layout.addRow('接收方(逗号分隔):', self.sub_edit)
        layout.addRow('长度(字节):', self.size_spin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def values(self):
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError('帧名称不能为空')
        fid = parse_hex(self.id_edit.text().strip())
        pub = self.pub_edit.currentText().strip()
        subs = [x.strip() for x in self.sub_edit.text().split(',') if x.strip()]
        return name, fid, pub, subs, self.size_spin.value()


class AddSignalDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('添加信号')
        layout = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 1023)
        self.len_spin = QSpinBox()
        self.len_spin.setRange(1, 64)
        self.len_spin.setValue(1)
        layout.addRow('信号名称:', self.name_edit)
        layout.addRow('起始 bit 偏移:', self.start_spin)
        layout.addRow('bit 大小:', self.len_spin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def values(self):
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError('信号名称不能为空')
        return name, self.start_spin.value(), self.len_spin.value()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('LIN_CFG_Editor.exe-%s' % APP_VERSION)
        self.resize(1180, 760)
        self.model = None
        self._loading = False
        self._pending_drop_row = -1
        self._build_ui()
        self._style()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 顶部
        top = QGroupBox('LDF 导入与生成')
        top_layout = QVBoxLayout(top)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel('网络:'))
        self.network_edit = QLineEdit('LIN1')
        self.network_edit.setMaximumWidth(80)
        row1.addWidget(self.network_edit)
        row1.addWidget(QLabel('  LDF 文件:'))
        self.ldf_edit = QLineEdit()
        self.ldf_edit.setReadOnly(True)
        self.ldf_edit.setPlaceholderText('选择要导入的 LDF 文件...')
        row1.addWidget(self.ldf_edit, 1)
        import_btn = QPushButton('导入 LDF')
        import_btn.clicked.connect(self.import_ldf)
        row1.addWidget(import_btn)
        top_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel('导出路径:'))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText('默认与导入 LDF 同目录')
        row2.addWidget(self.output_edit, 1)
        browse_btn = QPushButton('浏览...')
        browse_btn.clicked.connect(self.browse_output)
        row2.addWidget(browse_btn)
        self.gen_btn = QPushButton('生成三个文件')
        self.gen_btn.setMinimumHeight(34)
        self.gen_btn.clicked.connect(self.generate)
        row2.addWidget(self.gen_btn)
        top_layout.addLayout(row2)

        self.info_label = QLabel('请先导入 LDF 文件')
        self.info_label.setStyleSheet('color:#0078d4;')
        top_layout.addWidget(self.info_label)
        layout.addWidget(top)

        # 中央 tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_frame_tab(), 'ID / 帧')
        self.tabs.addTab(self._build_schedule_tab(), '调度表')
        self.tabs.addTab(self._build_sporadic_tab(), '零星帧')
        layout.addWidget(self.tabs, 1)

        self.statusBar().showMessage('就绪')

    def _build_frame_tab(self):
        widget = QWidget()
        splitter = QSplitter(Qt.Horizontal)

        # 左: ID 表格
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('ID(帧) 列表'))
        self.frame_table = QTableWidget(0, 6)
        self.frame_table.setHorizontalHeaderLabels(
            ['帧名称', 'LIN ID(hex)', '发送方', '接收方', '长度(字节)', '方向'])
        self.frame_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.frame_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.frame_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.frame_table.itemChanged.connect(self.on_frame_cell_changed)
        self.frame_table.itemSelectionChanged.connect(self.on_frame_selected)
        ll.addWidget(self.frame_table, 1)
        btn_row = QHBoxLayout()
        add_btn = QPushButton('添加 ID')
        add_btn.clicked.connect(self.add_frame)
        del_btn = QPushButton('删除 ID')
        del_btn.clicked.connect(self.del_frame)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        ll.addLayout(btn_row)
        splitter.addWidget(left)

        # 右: 信号 + 初始值
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel('信号列表 (选中 ID)'))
        self.signal_table = QTableWidget(0, 4)
        self.signal_table.setHorizontalHeaderLabels(['信号名称', '起始 bit 偏移', 'bit 大小', '默认值(hex)'])
        self.signal_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.signal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.signal_table.itemChanged.connect(self.on_signal_cell_changed)
        rl.addWidget(self.signal_table, 1)
        sbtn_row = QHBoxLayout()
        sadd_btn = QPushButton('添加信号')
        sadd_btn.clicked.connect(self.add_signal)
        sdel_btn = QPushButton('删除信号')
        sdel_btn.clicked.connect(self.del_signal)
        sbtn_row.addWidget(sadd_btn)
        sbtn_row.addWidget(sdel_btn)
        sbtn_row.addStretch(1)
        rl.addLayout(sbtn_row)

        init_group = QGroupBox('字节初始值 (十六进制, 空格分隔, 默认 0x00)')
        il = QHBoxLayout(init_group)
        self.init_edit = QLineEdit()
        self.init_edit.setFont(QFont('Consolas', 10))
        self.init_edit.editingFinished.connect(self.on_init_edited)
        il.addWidget(self.init_edit, 1)
        rl.addWidget(init_group)
        self.init_hint = QLabel('')
        self.init_hint.setStyleSheet('color:#777;')
        rl.addWidget(self.init_hint)
        splitter.addWidget(right)
        splitter.setSizes([560, 600])

        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.addWidget(splitter)
        return widget

    def _build_schedule_tab(self):
        widget = QWidget()
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('调度表列表'))
        self.schedule_table = DragDropTable(0, 2)
        self.schedule_table.setHorizontalHeaderLabels(['调度表名', '条目数'])
        self.schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.schedule_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.schedule_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.schedule_table.itemChanged.connect(self.on_schedule_cell_changed)
        self.schedule_table.itemSelectionChanged.connect(self.on_schedule_selected)
        self.schedule_table.rowsDropped.connect(self._on_schedule_dropped)
        self.schedule_table.dragFinished.connect(self._on_drag_finished)
        ll.addWidget(self.schedule_table, 1)
        b1 = QHBoxLayout()
        a1 = QPushButton('添加调度表')
        a1.clicked.connect(self.add_schedule)
        d1 = QPushButton('删除调度表')
        d1.clicked.connect(self.del_schedule)
        b1.addWidget(a1)
        b1.addWidget(d1)
        b1.addStretch(1)
        ll.addLayout(b1)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel('调度表条目 (选中调度表): 帧名 + tick 数'))
        self.entry_table = QTableWidget(0, 2)
        self.entry_table.setHorizontalHeaderLabels(['帧名', 'tick 数'])
        self.entry_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.entry_table.itemChanged.connect(self.on_entry_cell_changed)
        rl.addWidget(self.entry_table, 1)
        b2 = QHBoxLayout()
        a2 = QPushButton('添加条目')
        a2.clicked.connect(self.add_entry)
        d2 = QPushButton('删除条目')
        d2.clicked.connect(self.del_entry)
        b2.addWidget(a2)
        b2.addWidget(d2)
        b2.addStretch(1)
        rl.addLayout(b2)
        splitter.addWidget(right)
        splitter.setSizes([360, 760])

        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.addWidget(splitter)
        return widget

    def _build_sporadic_tab(self):
        widget = QWidget()
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('零星帧组列表'))
        self.sporadic_table = QTableWidget(0, 1)
        self.sporadic_table.setHorizontalHeaderLabels(['零星帧组名'])
        self.sporadic_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sporadic_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sporadic_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sporadic_table.itemChanged.connect(self.on_sporadic_cell_changed)
        self.sporadic_table.itemSelectionChanged.connect(self.on_sporadic_selected)
        ll.addWidget(self.sporadic_table, 1)
        b1 = QHBoxLayout()
        a1 = QPushButton('添加零星帧组')
        a1.clicked.connect(self.add_sporadic)
        d1 = QPushButton('删除零星帧组')
        d1.clicked.connect(self.del_sporadic)
        b1.addWidget(a1)
        b1.addWidget(d1)
        b1.addStretch(1)
        ll.addLayout(b1)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel('组内帧 (选中零星帧组, 共享一个时隙)'))
        self.spframe_table = QTableWidget(0, 1)
        self.spframe_table.setHorizontalHeaderLabels(['帧名'])
        self.spframe_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.spframe_table.itemChanged.connect(self.on_spframe_cell_changed)
        rl.addWidget(self.spframe_table, 1)
        b2 = QHBoxLayout()
        a2 = QPushButton('添加帧')
        a2.clicked.connect(self.add_spframe)
        d2 = QPushButton('删除帧')
        d2.clicked.connect(self.del_spframe)
        b2.addWidget(a2)
        b2.addWidget(d2)
        b2.addStretch(1)
        rl.addLayout(b2)
        hint = QLabel('注: 零星帧在生成 database 时被忽略(SPOR_TABLE_SIZE=0)。')
        hint.setStyleSheet('color:#c50;')
        rl.addWidget(hint)
        splitter.addWidget(right)
        splitter.setSizes([360, 760])

        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.addWidget(splitter)
        return widget

    def _style(self):
        self.setStyleSheet('''
            QMainWindow { background:#f5f5f5; }
            QGroupBox { font-weight:bold; border:1px solid #ccc; border-radius:4px; margin-top:8px; padding-top:12px; background:white; }
            QGroupBox::title { subcontrol-origin:margin; subcontrol-position:top left; padding:0 6px; }
            QLineEdit { border:1px solid #c8c8c8; border-radius:3px; padding:5px 8px; background:#fafafa; }
            QPushButton { border:1px solid #c6c6c6; border-radius:3px; padding:5px 14px; background:#e6e6e6; }
            QPushButton:hover { background:#d8d8d8; }
            QTableWidget { gridline-color:#e0e0e0; }
            QHeaderView::section { background:#f0f0f0; padding:4px; border:none; border-right:1px solid #ddd; border-bottom:1px solid #ddd; }
        ''')
        self.gen_btn.setStyleSheet(
            'QPushButton { background:#0078d4; color:white; border:none; border-radius:4px; font-weight:bold; }'
            'QPushButton:hover { background:#106ebe; } QPushButton:disabled { background:#999; }')

    # ------------------------------------------------------------- 导入/生成
    def import_ldf(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 LDF 文件', '', 'LDF Files (*.ldf);;All Files (*.*)')
        if not path:
            return
        try:
            self.model = parse_ldf(path)
        except Exception as exc:
            QMessageBox.critical(self, '导入失败', str(exc))
            return
        self.ldf_edit.setText(path)
        net = detect_lin_network(path)
        self.network_edit.setText(net)
        self.output_edit.setText(os.path.dirname(path))
        self._loading = True
        try:
            self.refresh_frame_table()
            self.refresh_schedule_table()
            self.refresh_sporadic_table()
        finally:
            self._loading = False
        # 导入后自动选中第一个 ID 并刷新信号表与初始值编辑框
        if self.model and self.model.frames:
            self.frame_table.setCurrentCell(0, 0)
            self.refresh_signal_table()
            self.refresh_init_edit()
        self._update_info()
        self.statusBar().showMessage('导入成功: %s' % path)
        QMessageBox.warning(
            self, '重要提示',
            '重要：请检查ID长度、信号长度、初始值等信息，\n确保与网络矩阵表格/LDF文件一致，若不一致，请修改！')

    def browse_output(self):
        start = self.output_edit.text() or (os.path.dirname(self.ldf_edit.text()) if self.ldf_edit.text() else os.path.expanduser('~'))
        path = QFileDialog.getExistingDirectory(self, '选择导出目录', start)
        if path:
            self.output_edit.setText(path)

    def generate(self):
        if not self.model:
            QMessageBox.warning(self, '提示', '请先导入 LDF 文件。')
            return
        self.on_init_edited()
        net = self.network_edit.text().strip().upper() or 'LIN1'
        if not re.match(r'^LIN\d+$', net):
            net = 'LIN1'
        self.model.network = net
        output_dir = self.output_edit.text().strip() or os.path.dirname(self.model.source_path)
        if not output_dir:
            QMessageBox.warning(self, '提示', '请选择导出目录。')
            return
        try:
            ok, msg, files = generate_files(self.model, output_dir)
        except Exception as exc:
            QMessageBox.critical(self, '生成失败', str(exc))
            return
        self._update_info()
        self.statusBar().showMessage(msg)
        detail = msg + '\n\n生成文件:\n' + '\n'.join(files)
        QMessageBox.information(self, '生成完成', detail)

    def _update_info(self):
        if not self.model:
            self.info_label.setText('请先导入 LDF 文件')
            return
        db = build_database(self.model)
        a = compute_addresses(self.model)
        self.info_label.setText(
            'ID 数量: %d | 调度表数量: %d | 零星帧组: %d | database 大小: %d 字节 | '
            'SCH_LEN=%d TICK_MAX=%d INIT=%d ID=%d DIR=%d LEN=%d' % (
                len(self.model.frames), len(self.model.schedules), len(self.model.sporadic),
                len(db), a['sch_len_start'], a['tick_max_start'], a['init_start'],
                a['id_start'], a['id_dir_start'], a['id_len_start']))

    # ----------------------------------------------------------- ID 表格
    def refresh_frame_table(self):
        self._loading = True
        try:
            self.frame_table.setRowCount(0)
            if not self.model:
                return
            self.frame_table.setRowCount(len(self.model.frames))
            for i, f in enumerate(self.model.frames):
                items = [
                    self._mk_item(f.name),
                    self._mk_item('0x%02X' % f.frame_id),
                    self._mk_item(f.publisher),
                    self._mk_item(format_subscribers(f.subscribers)),
                    self._mk_item(str(f.size)),
                    self._mk_item(f.direction_str(self.model.master), editable=False),
                ]
                for col, it in enumerate(items):
                    it.setData(Qt.UserRole, it.text())
                    self.frame_table.setItem(i, col, it)
        finally:
            self._loading = False

    def on_frame_cell_changed(self, item):
        if self._loading or not self.model:
            return
        i = item.row()
        col = item.column()
        if i >= len(self.model.frames):
            return
        f = self.model.frames[i]
        text = item.text().strip()
        old = item.data(Qt.UserRole) or ''
        try:
            if col == F_COL_NAME:
                if not text:
                    raise ValueError('帧名称不能为空')
                if text != old:
                    self._rename_frame(old, text)
                    f.name = text
            elif col == F_COL_ID:
                f.frame_id = parse_hex(text) & 0x3F
            elif col == F_COL_PUB:
                f.publisher = text
            elif col == F_COL_SUB:
                f.subscribers = [x.strip() for x in text.split(',') if x.strip()]
            elif col == F_COL_SIZE:
                f.resize(int(text))
                item.setText(str(f.size))
            item.setData(Qt.UserRole, text)
        except ValueError:
            item.setText(old)
            item.setData(Qt.UserRole, old)
        self._refresh_after_frame_change(i)

    def _rename_frame(self, old, new):
        for sch in self.model.schedules:
            for e in sch.entries:
                if e.frame_name == old:
                    e.frame_name = new
        for g in self.model.sporadic:
            for k, n in enumerate(g.frame_names):
                if n == old:
                    g.frame_names[k] = new

    def _refresh_after_frame_change(self, selected_row):
        self._update_info()
        self.refresh_schedule_table()
        self.refresh_sporadic_table()

    def on_frame_selected(self):
        row = self.frame_table.currentRow()
        if row >= 0 and self.model and row < len(self.model.frames):
            self.refresh_signal_table()
            self.refresh_init_edit()

    def add_frame(self):
        if not self.model:
            QMessageBox.warning(self, '提示', '请先导入 LDF 文件。')
            return
        dlg = AddFrameDialog(self.model.master, self.model.slaves, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            name, fid, pub, subs, size = dlg.values()
        except ValueError as e:
            QMessageBox.warning(self, '提示', str(e))
            return
        self.model.frames.append(Frame(name, fid, pub, subs, size))
        self.refresh_frame_table()
        self.refresh_schedule_table()
        self._update_info()

    def del_frame(self):
        row = self.frame_table.currentRow()
        if not self.model or row < 0 or row >= len(self.model.frames):
            return
        name = self.model.frames[row].name
        if QMessageBox.question(self, '确认', '确定删除 ID "%s" ?' % name) != QMessageBox.Yes:
            return
        self.model.frames.pop(row)
        for sch in self.model.schedules:
            sch.entries = [e for e in sch.entries if e.frame_name != name]
        for g in self.model.sporadic:
            g.frame_names = [n for n in g.frame_names if n != name]
        self.refresh_frame_table()
        self.refresh_signal_table()
        self.refresh_schedule_table()
        self.refresh_sporadic_table()
        self._update_info()

    # --------------------------------------------------------- 信号表格
    def _current_frame(self):
        row = self.frame_table.currentRow()
        if self.model and 0 <= row < len(self.model.frames):
            return self.model.frames[row]
        return None

    def refresh_signal_table(self):
        self._loading = True
        try:
            self.signal_table.setRowCount(0)
            f = self._current_frame()
            if not f:
                return
            self.signal_table.setRowCount(len(f.signals))
            for i, s in enumerate(f.signals):
                items = [
                    self._mk_item(s.name),
                    self._mk_item(str(s.start)),
                    self._mk_item(str(s.length)),
                    self._mk_item('0x%X' % s.init_value),
                ]
                for col, it in enumerate(items):
                    it.setData(Qt.UserRole, it.text())
                    self.signal_table.setItem(i, col, it)
        finally:
            self._loading = False

    def on_signal_cell_changed(self, item):
        if self._loading:
            return
        f = self._current_frame()
        if not f:
            return
        i = item.row()
        if i >= len(f.signals):
            return
        s = f.signals[i]
        text = item.text().strip()
        old = item.data(Qt.UserRole) or ''
        try:
            if item.column() == S_COL_NAME:
                if not text:
                    raise ValueError('名称不能为空')
                s.name = text
            elif item.column() == S_COL_START:
                s.start = int(text)
            elif item.column() == S_COL_LEN:
                s.length = int(text)
            elif item.column() == S_COL_INIT:
                s.init_value = parse_hex(text)
            item.setData(Qt.UserRole, text)
        except ValueError:
            item.setText(old)
            item.setData(Qt.UserRole, old)
            return
        # 起始偏移/位宽/初始值变化都会影响字节初始值, 同步重建并刷新下方字节框
        if item.column() in (S_COL_START, S_COL_LEN, S_COL_INIT):
            rebuild_init_bytes(f)
            self.refresh_init_edit()

    def add_signal(self):
        f = self._current_frame()
        if not f:
            QMessageBox.warning(self, '提示', '请先在左侧选中一个 ID。')
            return
        dlg = AddSignalDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            name, start, length = dlg.values()
        except ValueError as e:
            QMessageBox.warning(self, '提示', str(e))
            return
        f.signals.append(Signal(name, length, start))
        # 按起始 bit 偏移从小到大重新排序
        f.signals.sort(key=lambda s: s.start)
        self.refresh_signal_table()

    def del_signal(self):
        f = self._current_frame()
        row = self.signal_table.currentRow()
        if not f or row < 0 or row >= len(f.signals):
            return
        f.signals.pop(row)
        rebuild_init_bytes(f)
        self.refresh_signal_table()
        self.refresh_init_edit()

    # ----------------------------------------------------------- 初始值
    def refresh_init_edit(self):
        f = self._current_frame()
        if not f:
            self.init_edit.setText('')
            self.init_hint.setText('')
            return
        self.init_edit.setText(format_hex_bytes(f.init_bytes))
        self.init_hint.setText('共 %d 字节, 对应 database 初始值区' % f.size)

    def on_init_edited(self):
        f = self._current_frame()
        if not f:
            return
        text = self.init_edit.text().strip()
        try:
            vals = parse_hex_bytes(text)
        except ValueError:
            QMessageBox.warning(self, '提示', '初始值格式错误: 应为空格分隔的十六进制字节, 如 "00 00 00 00"')
            self.init_edit.setText(format_hex_bytes(f.init_bytes))
            return
        if len(vals) != f.size:
            QMessageBox.warning(self, '提示', '初始值字节数(%d) 与帧长度(%d) 不一致。' % (len(vals), f.size))
            self.init_edit.setText(format_hex_bytes(f.init_bytes))
            return
        f.init_bytes = vals
        # 字节初始值变更后, 反推每个信号的初始值并刷新信号表
        read_signal_init_values(f)
        self.refresh_signal_table()

    # --------------------------------------------------------- 调度表
    def refresh_schedule_table(self):
        self._loading = True
        try:
            self.schedule_table.setRowCount(0)
            if not self.model:
                return
            self.schedule_table.setRowCount(len(self.model.schedules))
            for i, s in enumerate(self.model.schedules):
                name_it = self._mk_item(s.name)
                name_it.setData(Qt.UserRole, s.name)
                self.schedule_table.setItem(i, 0, name_it)
                cnt_it = self._mk_item(str(len(s.entries)), editable=False)
                self.schedule_table.setItem(i, 1, cnt_it)
        finally:
            self._loading = False
        self.refresh_entry_table()

    def on_schedule_cell_changed(self, item):
        if self._loading or not self.model:
            return
        i = item.row()
        if item.column() == 0 and i < len(self.model.schedules):
            text = item.text().strip()
            if text:
                self.model.schedules[i].name = text
            else:
                item.setText(self.model.schedules[i].name)

    def on_schedule_selected(self):
        self.refresh_entry_table()

    def _on_schedule_dropped(self, src, dst):
        """拖放落点确定: 仅移动 model.schedules 元素; 表格在拖放结束后统一重建。"""
        if self._loading or not self.model:
            return
        if src < 0 or src >= len(self.model.schedules):
            return
        sched = self.model.schedules.pop(src)
        if dst > src:
            dst -= 1
        dst = max(0, min(dst, len(self.model.schedules)))
        self.model.schedules.insert(dst, sched)
        self._pending_drop_row = dst

    def _on_drag_finished(self):
        """拖放整体结束后, 从 model 全量重建调度表(条目数列据 model 实时计算)。"""
        if self._loading or not self.model:
            return
        self.refresh_schedule_table()
        dst = getattr(self, '_pending_drop_row', -1)
        if 0 <= dst < self.schedule_table.rowCount():
            self.schedule_table.setCurrentCell(dst, 0)
        self._pending_drop_row = -1
        self._update_info()

    def _current_schedule(self):
        row = self.schedule_table.currentRow()
        if self.model and 0 <= row < len(self.model.schedules):
            return self.model.schedules[row]
        return None

    def refresh_entry_table(self):
        self._loading = True
        try:
            self.entry_table.setRowCount(0)
            s = self._current_schedule()
            if not s:
                return
            self.entry_table.setRowCount(len(s.entries))
            for i, e in enumerate(s.entries):
                name_it = self._mk_item(e.frame_name)
                name_it.setData(Qt.UserRole, e.frame_name)
                self.entry_table.setItem(i, 0, name_it)
                tick_it = self._mk_item(str(e.tick))
                tick_it.setData(Qt.UserRole, str(e.tick))
                self.entry_table.setItem(i, 1, tick_it)
        finally:
            self._loading = False

    def on_entry_cell_changed(self, item):
        if self._loading:
            return
        s = self._current_schedule()
        if not s:
            return
        i = item.row()
        if i >= len(s.entries):
            return
        e = s.entries[i]
        text = item.text().strip()
        old = item.data(Qt.UserRole) or ''
        if item.column() == 0:
            if text:
                e.frame_name = text
            else:
                item.setText(old)
                return
        else:
            try:
                e.tick = max(1, int(text))
                item.setText(str(e.tick))
            except ValueError:
                item.setText(old)
                return
        item.setData(Qt.UserRole, item.text())
        self._update_schedule_count()
        self._update_info()

    def _update_schedule_count(self):
        row = self.schedule_table.currentRow()
        if self.model and 0 <= row < len(self.model.schedules):
            self.schedule_table.item(row, 1).setText(str(len(self.model.schedules[row].entries)))

    def add_schedule(self):
        if not self.model:
            QMessageBox.warning(self, '提示', '请先导入 LDF 文件。')
            return
        name, ok = self._ask_text('添加调度表', '调度表名称:', 'Schedule%d' % (len(self.model.schedules) + 1))
        if not ok:
            return
        self.model.schedules.append(Schedule(name))
        self.refresh_schedule_table()
        self._update_info()

    def del_schedule(self):
        row = self.schedule_table.currentRow()
        if not self.model or row < 0 or row >= len(self.model.schedules):
            return
        name = self.model.schedules[row].name
        if QMessageBox.question(self, '确认', '确定删除调度表 "%s" ?' % name) != QMessageBox.Yes:
            return
        self.model.schedules.pop(row)
        self.refresh_schedule_table()
        self._update_info()

    def add_entry(self):
        s = self._current_schedule()
        if not s:
            QMessageBox.warning(self, '提示', '请先在左侧选中一个调度表。')
            return
        dlg = QDialog(self)
        dlg.setWindowTitle('添加调度表条目')
        form = QFormLayout(dlg)
        frame_combo = QComboBox()
        for f in self.model.frames:
            frame_combo.addItem(f.name)
        tick_spin = QSpinBox()
        tick_spin.setRange(1, 65535)
        tick_spin.setValue(1)
        form.addRow('帧名:', frame_combo)
        form.addRow('tick 数:', tick_spin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() != QDialog.Accepted:
            return
        s.entries.append(ScheduleEntry(frame_combo.currentText(), tick_spin.value()))
        self.refresh_entry_table()
        self._update_schedule_count()
        self._update_info()

    def del_entry(self):
        s = self._current_schedule()
        row = self.entry_table.currentRow()
        if not s or row < 0 or row >= len(s.entries):
            return
        s.entries.pop(row)
        self.refresh_entry_table()
        self._update_schedule_count()
        self._update_info()

    # --------------------------------------------------------- 零星帧
    def refresh_sporadic_table(self):
        self._loading = True
        try:
            self.sporadic_table.setRowCount(0)
            if not self.model:
                return
            self.sporadic_table.setRowCount(len(self.model.sporadic))
            for i, g in enumerate(self.model.sporadic):
                it = self._mk_item(g.name)
                it.setData(Qt.UserRole, g.name)
                self.sporadic_table.setItem(i, 0, it)
        finally:
            self._loading = False
        self.refresh_spframe_table()

    def on_sporadic_cell_changed(self, item):
        if self._loading or not self.model:
            return
        i = item.row()
        if i < len(self.model.sporadic):
            text = item.text().strip()
            if text:
                self.model.sporadic[i].name = text
            else:
                item.setText(self.model.sporadic[i].name)

    def on_sporadic_selected(self):
        self.refresh_spframe_table()

    def _current_sporadic(self):
        row = self.sporadic_table.currentRow()
        if self.model and 0 <= row < len(self.model.sporadic):
            return self.model.sporadic[row]
        return None

    def refresh_spframe_table(self):
        self._loading = True
        try:
            self.spframe_table.setRowCount(0)
            g = self._current_sporadic()
            if not g:
                return
            self.spframe_table.setRowCount(len(g.frame_names))
            for i, n in enumerate(g.frame_names):
                it = self._mk_item(n)
                it.setData(Qt.UserRole, n)
                self.spframe_table.setItem(i, 0, it)
        finally:
            self._loading = False

    def on_spframe_cell_changed(self, item):
        if self._loading:
            return
        g = self._current_sporadic()
        if not g:
            return
        i = item.row()
        if i >= len(g.frame_names):
            return
        text = item.text().strip()
        if text:
            g.frame_names[i] = text
        else:
            item.setText(g.frame_names[i])
        item.setData(Qt.UserRole, item.text())

    def add_sporadic(self):
        if not self.model:
            QMessageBox.warning(self, '提示', '请先导入 LDF 文件。')
            return
        name, ok = self._ask_text('添加零星帧组', '零星帧组名称:', 'SPORADIC_%d' % (len(self.model.sporadic) + 1))
        if not ok:
            return
        self.model.sporadic.append(SporadicGroup(name))
        self.refresh_sporadic_table()

    def del_sporadic(self):
        row = self.sporadic_table.currentRow()
        if not self.model or row < 0 or row >= len(self.model.sporadic):
            return
        self.model.sporadic.pop(row)
        self.refresh_sporadic_table()

    def add_spframe(self):
        g = self._current_sporadic()
        if not g:
            QMessageBox.warning(self, '提示', '请先在左侧选中一个零星帧组。')
            return
        if not self.model.frames:
            QMessageBox.warning(self, '提示', '请先添加 ID。')
            return
        dlg = QDialog(self)
        dlg.setWindowTitle('添加零星帧')
        form = QFormLayout(dlg)
        combo = QComboBox()
        for f in self.model.frames:
            combo.addItem(f.name)
        form.addRow('帧名:', combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() != QDialog.Accepted:
            return
        g.frame_names.append(combo.currentText())
        self.refresh_spframe_table()

    def del_spframe(self):
        g = self._current_sporadic()
        row = self.spframe_table.currentRow()
        if not g or row < 0 or row >= len(g.frame_names):
            return
        g.frame_names.pop(row)
        self.refresh_spframe_table()

    # ----------------------------------------------------------- 工具
    def _mk_item(self, text, editable=True):
        it = QTableWidgetItem(text)
        if not editable:
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        return it

    def _ask_text(self, title, label, default=''):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        edit = QLineEdit(default)
        form.addRow(label, edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() != QDialog.Accepted:
            return None, False
        return edit.text().strip(), True


def main():
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
