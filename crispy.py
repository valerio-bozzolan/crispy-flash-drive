#!/usr/bin/env python3
import sys

import argparse
from PyQt5.QtDBus import QDBusConnection, QDBusMessage
from PyQt5.QtGui import QIcon, QPixmap  # QFontMetrics, QFont
from json import JSONDecodeError
from multiprocessing import Lock

import json
import subprocess
# noinspection PyUnresolvedReferences
from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import QDateTime, QSize, pyqtSlot, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget, QPushButton, QDesktopWidget, QMainWindow, QGridLayout, QLabel, \
	QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QProgressBar
from dataclasses import dataclass
from typing import List


def make_button(text: str, action, icon=None, tooltip=''):
	button = QPushButton(text)
	if len(tooltip) > 0:
		button.setToolTip(tooltip)
	if icon is not None:
		button.setIcon(icon)
	button.resize(button.sizeHint())
	# Works perfectly but it's unresolved, yeah...
	# noinspection PyUnresolvedReferences
	button.clicked.connect(action)
	return button


class Toaster(QMainWindow):

	# Only works if placed HERE
	progress_signal = pyqtSignal(str, int)

	def __init__(self, argv: List[str]):
		parser = argparse.ArgumentParser(description='"Toast" Linux distros or other ISO files on USB drives.')
		parser.add_argument('json', nargs='?', type=str, help="Path to JSON file with available distros")
		parser.add_argument('-k', '--kiosk', action='store_true',
			help="Enable kiosk mode (ignore any attempt to close)")
		parser.set_defaults(kiosk=False)
		args = parser.parse_args(argv[1:])
		self.kiosk = args.kiosk
		self.threads = set()
		filename = args.json

		if filename is None:
			# noinspection PyArgumentList
			filename = QFileDialog.getOpenFileName(None, "Select JSON data file", '', 'JSON file (*.json)')
			filename = filename[0]

		# Pressing "cancel" in the file dialog
		if filename == '':
			print("Select a file")
			exit(1)

		json_distros = None
		distros = []
		logos = dict()

		try:
			with open(filename) as file:
				json_distros = json.loads(file.read())
		except FileNotFoundError:
			print(f"Cannot open {filename} for reading")
			exit(1)
		except JSONDecodeError as e:
			print(f"JSON decode error in {filename} on line {e.lineno}, col {e.colno}: {e.msg}")
			exit(1)

		for json_distro in json_distros:
			if json_distro['logo'] in logos:
				rendered_logo = logos[json_distro['logo']]
			else:
				size = QSize()
				size.setHeight(100)
				size.setWidth(100)
				rendered_logo = QIcon(json_distro['logo']).pixmap(size)
				logos[json_distro['logo']] = rendered_logo
			distro = Distro(json_distro['name'], json_distro['file'], json_distro['logo'], rendered_logo, json_distro['description'])
			distros.append(distro)

		# noinspection PyArgumentList
		super().__init__()
		self.progress_signal.connect(self.toaster_signaled)
		self.status_bar = self.statusBar()
		# noinspection PyArgumentList
		self.progress_area = QVBoxLayout()
		self.distro_widget = DistroList(distros)
		self.drives_list = DriveList()
		self.window()

		# noinspection PyArgumentList
		dbus = QDBusConnection.systemBus()
		dbus.connect('org.freedesktop.UDisks2', '/org/freedesktop/UDisks2', 'org.freedesktop.DBus.ObjectManager', 'InterfacesAdded', self.handle_dbus_add)
		dbus.connect('org.freedesktop.UDisks2', '/org/freedesktop/UDisks2', 'org.freedesktop.DBus.ObjectManager', 'InterfacesRemoved', self.handle_dbus_remove)

	def window(self):
		# noinspection PyArgumentList
		the_widget = QWidget()
		the_layout = QVBoxLayout()
		the_widget.setLayout(the_layout)
		self.central(the_layout)
		self.setCentralWidget(the_widget)

	def central(self, central: QVBoxLayout):
		self.set_status('Ready to toast')
		self.resize(300, 550)
		self.center()
		self.setWindowTitle('Crispy Flash Drives')

		toast_btn = make_button('Toast!', self.toast_clicked)
		cancel_btn = make_button('Cancel', self.cancel_clicked)
		refresh_btn = make_button('Refresh', self.refresh_clicked)

		# Distro selector
		central.addWidget(self.distro_widget, 1, QtCore.Qt.AlignTop)
		central.addStretch()

		# Label and selection area
		central.addWidget(QLabel('Flash drive'), 0, QtCore.Qt.AlignLeft)
		central.addWidget(self.drives_list, 0, QtCore.Qt.AlignBottom)

		# Button area
		# noinspection PyArgumentList
		button_area = QWidget()
		button_grid = QHBoxLayout()
		button_area.setLayout(button_grid)
		button_grid.addStretch()  # This is done twice to center buttons horizontally
		# noinspection PyArgumentList
		button_grid.addWidget(toast_btn)
		# noinspection PyArgumentList
		button_grid.addWidget(cancel_btn)
		# noinspection PyArgumentList
		button_grid.addWidget(refresh_btn)
		button_grid.addStretch()

		# noinspection PyArgumentList
		progress_widget = QWidget()
		progress_widget.setLayout(self.progress_area)
		central.addWidget(progress_widget, 0, QtCore.Qt.AlignBottom)

		# Again, valid arguments that PyCharm doesn't understand
		# noinspection PyArgumentList
		central.addWidget(button_area)  # span both columns (and one row)

		self.show()

	def closeEvent(self, event):
		if self.kiosk:
			event.ignore()

	def center(self):
		main_window = self.frameGeometry()
		main_window.moveCenter(QDesktopWidget().availableGeometry().center())
		self.move(main_window.topLeft())

	def set_status(self, status: str):
		self.status_bar.showMessage(status)

	def toast_clicked(self):
		# TODO: despite clearSelection(), this is still selected... Check flags? Check toasting_devices?
		selected: DriveListItem = self.drives_list.currentItem()
		if selected is None:
			msg_box = QMessageBox(self)
			msg_box.setIcon(QMessageBox.Warning)
			msg_box.setText('Select a flash drive to toast')
			msg_box.setStandardButtons(QMessageBox.Ok)
			msg_box.exec()
		else:
			# Prevent clicking on it again
			self.drives_list.set_toasting(selected)
			self.drives_list.clearSelection()

			# Create progress bar
			progress = QProgressBar()
			self.progress_area.addWidget(progress, 0, QtCore.Qt.AlignTop)

			# Start thread
			thread = ToastThread(selected.devstring, selected.devpath, self.distro_widget.get_current(), self.progress_signal)
			self.threads.add(thread)
			thread.start()

	def cancel_clicked(self):
		print("cancel")  # TODO: delete this

	def refresh_clicked(self):
		self.drives_list.refresh()

	@pyqtSlot(str, int, name='toaster_signaled')
	def toaster_signaled(self, devstr: str, written: int):
		print(f"Signal signaled: {written} bytes written on {devstr}")

	# Good example (the only one that exists, actually): https://stackoverflow.com/q/38142809
	@pyqtSlot(QDBusMessage, name='handle_dbus_add')
	def handle_dbus_add(self, msg: QDBusMessage):
		if 'org.freedesktop.UDisks2.Drive' in msg.arguments()[1]:
			vendor = msg.arguments()[1]['org.freedesktop.UDisks2.Drive']['Vendor']
			model = msg.arguments()[1]['org.freedesktop.UDisks2.Drive']['Model']
			# serial = msg.arguments()[1]['org.freedesktop.UDisks2.Drive']['Serial']
			print(f"Dbus detected new device: {vendor} {model}")
			self.drives_list.refresh()

	@pyqtSlot(QDBusMessage, name='handle_dbus_remove')
	def handle_dbus_remove(self, msg: QDBusMessage):
		if 'org.freedesktop.UDisks2.Drive' in msg.arguments()[1]:
			print(f"Dbus detected device removal")
			self.drives_list.refresh()


@dataclass
class Distro:
	name: str
	file: str
	logo: str
	rendered_logo: QPixmap
	description: str


class DistroList(QWidget):
	def __init__(self, distros: List[Distro]):
		# noinspection PyArgumentList
		super().__init__()
		self.list = distros
		self.position = 0

		grid = QGridLayout()
		# Three columns: sides fixed, center can expand
		grid.setColumnStretch(0, 0)
		grid.setColumnStretch(1, 1)
		grid.setColumnStretch(2, 0)
		self.setLayout(grid)

		icon = QIcon()
		# TODO: better shortcuts than Alt+P and Alt+N
		# TODO: no text, just buttons. LARGE buttons.
		# noinspection PyArgumentList
		grid.addWidget(make_button('&Previous', self.scroll_left, icon.fromTheme('arrow-left')), 1, 0)

		self.distro_widget = DistroView(distros[0])
		# noinspection PyArgumentList
		grid.addWidget(self.distro_widget, 1, 1)

		# noinspection PyArgumentList
		grid.addWidget(make_button('&Next', self.scroll_right, icon.fromTheme('arrow-right')), 1, 2)

	def scroll_left(self):
		self.position -= 1
		if self.position < 0:
			self.position = len(self.list) - 1
		self.distro_widget.set_distro(self.list[self.position])

	def scroll_right(self):
		self.position += 1
		self.position %= len(self.list)
		self.distro_widget.set_distro(self.list[self.position])

	def get_current(self) -> Distro:
		return self.list[self.position]


class DistroView(QWidget):
	def __init__(self, distro: Distro):
		# noinspection PyArgumentList
		super().__init__()
		stack = QVBoxLayout()
		self.setLayout(stack)
		self.distro = None

		self.icon_widget = QLabel(self)
		self.icon_widget.setAlignment(QtCore.Qt.AlignCenter)
		# noinspection PyArgumentList
		stack.addWidget(self.icon_widget)
		self.title_widget = QLabel(self)
		self.title_widget.setAlignment(QtCore.Qt.AlignCenter)
		# noinspection PyArgumentList
		stack.addWidget(self.title_widget)
		self.description_widget = QLabel(self)
		# self.description_widget.setAlignment(QtCore.Qt.AlignCenter)
		self.description_widget.setWordWrap(True)
		# noinspection PyArgumentList
		stack.addWidget(self.description_widget)
		stack.addStretch()

		self.set_distro(distro)

	def set_distro(self, distro: Distro):
		self.distro = distro
		self.title_widget.setText(f"<h2>{distro.name}</h2>")
		self.description_widget.setText(distro.description)
		self.icon_widget.setPixmap(distro.rendered_logo)


class DriveList(QListWidget):
	def __init__(self):
		# noinspection PyArgumentList
		super().__init__()
		self.devices = dict()
		self.toasting_lock = Lock()
		self.toasting = dict()
		self.system_drives = set()
		# Enable to limit DriveList height (e.g. to 6 lines)
		# self.setMaximumHeight(QFontMetrics(QFont()).height() * 6)

		lsblk = self.do_lsblk()
		for device in lsblk["blockdevices"]:
			print(f"Detected /dev/{device['name']} as system drive")
			self.system_drives.add(device['name'])

	def refresh(self):
		lsblk = self.do_lsblk()
		detected_devices = set()
		for device in lsblk["blockdevices"]:
			if device['name'] not in self.system_drives:
				detected_devices.add(self.get_devstring(device))

		if detected_devices == self.devices.keys():
			print("No changes")
		else:
			print("Drives list changed")
			# Can't delete devices while iterating over list, so we need to replace it
			updated_devices_dict = dict()

			for device in self.devices:
				if device in detected_devices:
					print(f"Still there: {device}")
					updated_devices_dict[device] = self.devices[device]
				else:
					print(f"Gone: {device}")
					self.takeItem(self.row(self.devices[device]))
					self.unset_toasting(device)  # Why? To get rid of any reference to QListViewItem

			for device in detected_devices:
				path = device[device.rfind('(')+1:device.rfind(')')]
				if device not in self.devices:
					print(f"New: {device}")
					item = DriveListItem(self, device, path)
					updated_devices_dict[device] = item

			self.devices = updated_devices_dict

	def get_devstring(self, device: dict):
		vendor = device['vendor'].strip()
		model = device['model'].strip()
		size = self.pretty_size(device['size'])
		path = self.get_devpath(device)
		# serial = device['serial'].strip() + ', '
		if vendor == "ATA":
			vendor = ''
		else:
			vendor += ' '

		devstring = f'{vendor}{model}, {size} ({path})'

		while '  ' in devstring:
			devstring = devstring.replace('  ', ' ')

		return devstring

	@staticmethod
	def get_devpath(device: dict):
		return f"/dev/{device['name']}"

	def set_toasting(self, item: QListWidgetItem):
		with self.toasting_lock:
			drive = item.text()
			item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEnabled)  # ~QtCore.Qt.ItemIsSelectable is redundant
			self.toasting[drive] = item

	def unset_toasting(self, device: str):
		with self.toasting_lock:
			try:
				item = self.toasting[device]
				del self.toasting[device]
				item.setFlags(item.flags() | QtCore.Qt.ItemIsEnabled)
			except KeyError:
				pass

	@staticmethod
	def pretty_size(ugly_size: str) -> str:
		unit = ugly_size[-1:]
		size = ugly_size[:-1].replace(',', '.')
		return f"{size} {unit}iB"

	@staticmethod
	def do_lsblk():
		lsblk = subprocess.run(['lsblk', '-S', '-J', '-oNAME,VENDOR,MODEL,SIZE,SERIAL'], stdout=subprocess.PIPE)
		return json.loads(lsblk.stdout.decode('utf-8'))


class DriveListItem(QListWidgetItem):
	def __init__(self, parent: QWidget, devstring: str, devpath: str):
		super().__init__(parent)
		self.devstring = devstring
		self.devpath = devpath
		self.setText(devstring)
		icon = QIcon()
		self.setIcon(icon.fromTheme("drive-removable-media"))


class ToastThread(QThread):
	def __init__(self, devstring: str, path: str, distro: Distro, signal: pyqtSignal):
		QThread.__init__(self)
		self.begin_time = QDateTime()
		self.end_time = QDateTime()
		self.signal = signal
		self.devstring = devstring
		self.path = path
		self.distro = distro

	def __del__(self):
		self.wait()

	def run(self):
		self.begin_time.currentDateTime()
		for i in range(1, 100):
			self.signal.emit(self.devstring, 4096)
			self.sleep(1)
		self.end_time.currentDateTime()


if __name__ == '__main__':
	app = QApplication(sys.argv)
	ex = Toaster(app.arguments())
	sys.exit(app.exec_())
