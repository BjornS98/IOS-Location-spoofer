import sys
import os
import time
import json
import math
import random
import ctypes
import asyncio
import threading
from ctypes import wintypes
from PyQt5.QtGui import QIcon
from PyQt5 import QtCore, QtWidgets, QtWebEngineWidgets, QtWebChannel
from PyQt5.QtCore import QUrl, QObject, pyqtSlot, QTimer
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QDockWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QComboBox, QPushButton, QLabel, QDoubleSpinBox,
    QCheckBox, QMessageBox, QFileDialog
)
from Server import get_all_tunnels, update_location_over_tunnel, start_tunneld_server
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    return os.path.join(base_path, relative_path)

class WebPage(QtWebEngineWidgets.QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        pass

class Bridge(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._parent = parent

    @pyqtSlot(float, float)
    def sendCoordinates(self, lat, lon):
        self._parent.on_js_coordinates(lat, lon)

    @pyqtSlot(float, float)
    def addRoutePoint(self, lat, lon):
        self._parent.on_js_route_point(lat, lon)

    @pyqtSlot()
    def clearRouteInPython(self):
        self._parent.clear_route()

    @pyqtSlot()
    def pauseRouteIfActive(self):
        self._parent.pause_route_if_active()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.resuming_to_target = False
        self.resume_from_lat = None
        self.resume_from_lon = None
        self.setWindowTitle("GPS Spoofer")
        self.resize(1000, 700)
        self.current_lat = 48.8584
        self.current_lon = 2.2945
        self._pending_lat = self.current_lat
        self._pending_lon = self.current_lon
        self.speed = 5.0
        self.route_points = []
        self.route_index = 0
        self.loop_back = False
        self.simulation_active = False
        self.up_pressed = self.down_pressed = self.left_pressed = self.right_pressed = False
        self.device_connection_task = None
        self.location_simulation = None
        self.current_tunnel_params = None
        self.location_lock = threading.Lock()


        self.timer = QTimer(self)
        self.timer.timeout.connect(self.handle_movement)
        self.timer.start(100)
        self.route_timer = QTimer(self)
        self.route_timer.timeout.connect(self.advance_route)

        self._init_map_view()
        self._init_settings_dock()
        self._init_menu()

        # CREATE EVENT LOOP FIRST!
        self._event_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._event_loop.run_forever, daemon=True)
        self._loop_thread.start()

        # THEN connect the combobox signal (not in _init_settings_dock), after the event loop is created!
        self.tunnel_combo.currentIndexChanged.connect(self.on_tunnel_changed)

        self.refresh_tunnels_ui()


        # Optional: Periodic location resend to iPhone for stability
        self._event_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._event_loop.run_forever, daemon=True)
        self._loop_thread.start()

        QtWidgets.QApplication.instance().installEventFilter(self)

    def _init_map_view(self):
        self.view = QtWebEngineWidgets.QWebEngineView()
        self.setCentralWidget(self.view)
        page = WebPage(self.view)
        self.view.setPage(page)

        channel = QtWebChannel.QWebChannel(self)
        self.bridge = Bridge(self)
        channel.registerObject('bridge', self.bridge)
        page.setWebChannel(channel)

        html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "Assets/Map.html"))
        self.view.load(QUrl.fromLocalFile(html_path))
        self.view.loadFinished.connect(self.inject_initial_js_values)

    def inject_initial_js_values(self):
        self.view.page().runJavaScript(f"walkSpeed = {self.speed};")
        self.view.page().runJavaScript(f"startLat = {self.current_lat};")
        self.view.page().runJavaScript(f"startLon = {self.current_lon};")
        self.view.page().runJavaScript("initMap();")

    def _init_settings_dock(self):
        self.settings_dock = QtWidgets.QDockWidget("Settings", self)
        self.settings_dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea)
        # self.settings_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable | QtWidgets.QDockWidget.DockWidgetMovable)
        self.settings_dock.setTitleBarWidget(QWidget())
        widget = QtWidgets.QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10,10,10,10)
        layout.setSpacing(15)

        # Phone connection
        serial_grp = QGroupBox("Phone Connection")
        form = QFormLayout(serial_grp)
        self.tunnel_combo = QComboBox()
        form.addRow("Tunnel:", self.tunnel_combo)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_tunnels_ui)

        self.btn_start_sim = QPushButton("Start Simulation")
        self.btn_stop_sim = QPushButton("Stop Simulation")

        self.btn_start_sim.clicked.connect(self.start_simulation)
        self.btn_stop_sim.clicked.connect(self.stop_simulation)

        # Set default states: simulation stopped
        self.btn_start_sim.setEnabled(True)
        self.btn_stop_sim.setEnabled(False)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(btn_refresh)
        actions_layout.addWidget(self.btn_start_sim)
        actions_layout.addWidget(self.btn_stop_sim)
        actions_widget = QWidget()
        actions_widget.setLayout(actions_layout)
        form.addRow("Actions:", actions_widget)
        layout.addWidget(serial_grp)

        # Walking Speed
        speed_grp = QGroupBox("Walking Speed")
        h2 = QHBoxLayout(speed_grp); h2.setContentsMargins(10,5,10,5)
        self.speed_spin = QDoubleSpinBox(); self.speed_spin.setRange(0.1,50); self.speed_spin.setValue(self.speed)
        self.speed_spin.valueChanged.connect(self.on_speed_changed)
        h2.addWidget(QLabel("Speed (km/h):")); h2.addWidget(self.speed_spin); h2.addStretch()
        layout.addWidget(speed_grp)

        # Route Controls
        route_grp = QGroupBox("Route")
        vbox = QVBoxLayout(route_grp)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()
        self.select_route_btn = QPushButton("Select Route")
        self.start_route_btn = QPushButton("Start Route")
        self.pause_route_btn = QPushButton("Pause Route")
        self.resume_route_btn = QPushButton("Resume Route")
        self.stop_route_btn = QPushButton("Stop Route")
        self.loop_route_cb = QCheckBox("Loop Route")
        self.save_route_btn = QPushButton("Save Route")
        self.load_route_btn = QPushButton("Load Route")
        self.show_route_btn = QPushButton("Show Route")
        # initial disabled
        for btn in (self.start_route_btn, self.pause_route_btn, self.resume_route_btn, self.stop_route_btn, self.loop_route_cb, self.save_route_btn, self.show_route_btn):
            btn.setEnabled(False)
        # Row 1: core route controls
        for btn in (
        self.select_route_btn, self.start_route_btn, self.pause_route_btn, self.resume_route_btn, self.stop_route_btn):
            row1.addWidget(btn)
        # Row 2: additional features
        for btn in (self.save_route_btn, self.load_route_btn, self.show_route_btn, self.loop_route_cb):
            row2.addWidget(btn)

        vbox.addLayout(row1)
        vbox.addLayout(row2)

        layout.addWidget(route_grp)
        layout.addStretch()
        self.settings_dock.setWidget(widget)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.settings_dock)
        # connect signals
        self.select_route_btn.clicked.connect(self.select_route)
        self.start_route_btn.clicked.connect(self.start_route)
        self.pause_route_btn.clicked.connect(self.pause_route)
        self.resume_route_btn.clicked.connect(self.resume_route)
        self.stop_route_btn.clicked.connect(self.stop_route)
        self.save_route_btn.clicked.connect(self.save_route)
        self.load_route_btn.clicked.connect(self.load_route)
        self.show_route_btn.clicked.connect(self.teleport_to_route_start)

    def _init_menu(self):
        m = self.menuBar().addMenu("View")
        m.addAction(self.settings_dock.toggleViewAction())
        self.joystick_action = QtWidgets.QAction("Joystick", self, checkable=True)
        self.joystick_action.toggled.connect(self.on_joystick_toggled)
        m.addAction(self.joystick_action)
        self.view.loadFinished.connect(lambda _: self.joystick_action.setChecked(True))
        self.searchbar_action = QtWidgets.QAction("Search Bar", self, checkable=True)
        self.searchbar_action.setChecked(True)
        self.searchbar_action.toggled.connect(self.on_searchbar_toggled)
        m.addAction(self.searchbar_action)
        self.darkmode_action = QtWidgets.QAction("Dark Mode", self, checkable=True)
        self.darkmode_action.toggled.connect(self.darkmode_toggled)
        m.addAction(self.darkmode_action)

    def on_tunnel_changed(self, index):
        if (
                not hasattr(self, '_tunnel_map')
                or not self._tunnel_map
                or index < 0
                or index >= len(self._tunnel_map)
        ):
            return  # Ignore bad indices!
        udid, rsd_host, rsd_port = self._tunnel_map[index]
        self._setup_device_connection(udid, rsd_host, rsd_port)

    def _setup_device_connection(self, udid, rsd_host, rsd_port):
        # Clean up any previous
        if self.device_connection_task is not None:
            self.device_connection_task.cancel()
            self.device_connection_task = None
        # Save tunnel params
        self.current_tunnel_params = (udid, rsd_host, rsd_port)
        # Launch persistent connection in event loop
        async def run_device_connection():
            print("[DEBUG] Device connection loop started")
            try:
                async with RemoteServiceDiscoveryService((rsd_host, rsd_port)) as sp_rsd:
                    with DvtSecureSocketProxyService(sp_rsd) as dvt:
                        self.location_simulation = LocationSimulation(dvt)
                        while True:
                            # Only send GPS if simulation is active!
                            if getattr(self, "simulation_active", False):
                                with self.location_lock:
                                    lat, lon = self._pending_lat, self._pending_lon
                                if lat is not None and lon is not None:
                                    # Add 5–10 cm random noise
                                    noise_lat = random.uniform(-0.0000009, 0.0000009)
                                    noise_lon = random.uniform(-0.0000009, 0.0000009)
                                    noisy_lat = lat + noise_lat
                                    noisy_lon = lon + noise_lon
                                    self.location_simulation.set(noisy_lat, noisy_lon)

                            sleep = random.uniform(0.8, 1.3)
                            await asyncio.sleep(sleep)
            except Exception as e:
                print(f"[DEBUG] Device connection lost: {e}")
                self.location_simulation = None

        # Schedule in event loop
        self.device_connection_task = asyncio.run_coroutine_threadsafe(
            run_device_connection(), self._event_loop
        )

    def send_location_to_selected_tunnel(self, lat, lon):
        # print(f"[DEBUG] Setting new pending lat/lon: {lat}, {lon}")
        with self.location_lock:
            self._pending_lat = lat
            self._pending_lon = lon

    def refresh_tunnels_ui(self):
        tunnels, _ = get_all_tunnels()
        self.tunnel_combo.blockSignals(True)
        self.tunnel_combo.clear()
        self._tunnel_map = []
        for udid, tunnel_list in tunnels.items():
            for t in tunnel_list:
                label = f"UDID: {udid} | Host: {t['tunnel-address']} | Port: {t['tunnel-port']}"
                self.tunnel_combo.addItem(label)
                self._tunnel_map.append((udid, t['tunnel-address'], t['tunnel-port']))
        self.tunnel_combo.blockSignals(False)
        # Auto-select the first tunnel if available, triggers on_tunnel_changed
        self.tunnel_combo.setCurrentIndex(-1)  # Deselect everything
        if self.tunnel_combo.count() > 0:
            self.tunnel_combo.setCurrentIndex(0)  # This will emit currentIndexChanged

    def start_simulation(self):
        # Ensure tunnel is still active
        if not self.current_tunnel_params:
            QMessageBox.warning(self, "No Tunnel", "Please select a tunnel before starting simulation.")
            self.simulation_active = False
            return

        udid, rsd_host, rsd_port = self.current_tunnel_params
        tunnels, _ = get_all_tunnels()
        valid = False
        for t_udid, tunnel_list in tunnels.items():
            for t in tunnel_list:
                if t_udid == udid and t['tunnel-address'] == rsd_host and t['tunnel-port'] == rsd_port:
                    valid = True
                    break
            if valid:
                break

        if not valid:
            QMessageBox.critical(self, "Tunnel Not Active",
                                 "The selected tunnel is not active. Please refresh and select an available tunnel.")
            self.refresh_tunnels_ui()
            self.simulation_active = False
            return

        # Tunnel is valid, start simulation
        self.simulation_active = True
        self.btn_start_sim.setEnabled(False)
        self.btn_stop_sim.setEnabled(True)
        print("Simulation started. Route simulation is now permitted.")

    def stop_simulation(self):
        if not self.current_tunnel_params:
            QMessageBox.information(self, "No Tunnel", "No tunnel selected or active.")
            self.simulation_active = False
            return

        udid, rsd_host, rsd_port = self.current_tunnel_params

        async def clear_location():
            try:
                async with RemoteServiceDiscoveryService((rsd_host, rsd_port)) as sp_rsd:
                    with DvtSecureSocketProxyService(sp_rsd) as dvt:
                        LocationSimulation(dvt).clear()
                        print("Location Cleared Successfully")
            except Exception as e:
                print(f"Failed to clear location: {e}")

        # Schedule clearing on the event loop
        if hasattr(self, "_event_loop"):
            asyncio.run_coroutine_threadsafe(clear_location(), self._event_loop)

        self.simulation_active = False
        self.btn_start_sim.setEnabled(True)
        self.btn_stop_sim.setEnabled(False)
        # Optionally disable UI elements, reset fields, etc.
        print("Simulation stopped and GPS cleared.")

    def closeEvent(self, event):
        # Clean up background asyncio loop
        if hasattr(self, "_event_loop"):
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
            self._loop_thread.join()
        event.accept()

    def update_position(self, lat, lon):
        self.current_lat, self.current_lon = lat, lon
        js = f"marker.setLatLng([{lat}, {lon}]); map.panTo([{lat}, {lon}]);"
        self.view.page().runJavaScript(js)
        self.send_location_to_selected_tunnel(lat, lon)

    def select_route(self):
        self.route_points.clear()
        self.pause_route_btn.setEnabled(False)
        self.resume_route_btn.setEnabled(False)
        self.stop_route_btn.setEnabled(False)
        self.loop_route_cb.setEnabled(False)
        self.start_route_btn.setEnabled(False)
        self.save_route_btn.setEnabled(False)
        self.show_route_btn.setEnabled(False)
        # self.joystick_action.setEnabled(False)
        self.view.page().runJavaScript("clearRouteLine();")
        self.view.page().runJavaScript("enableRouteSelection();")

    def on_js_route_point(self, lat, lon):
        self.route_points.append((lat, lon))
        self.start_route_btn.setEnabled(True)
        self.loop_route_cb.setEnabled(True)
        self.save_route_btn.setEnabled(True)
        self.show_route_btn.setEnabled(True)

    def start_route(self):
        if not self.route_points:
            return
        self.loop_back = False
        self.view.page().runJavaScript("disableRouteSelection();")
        self.route_index = 0
        self.start_route_btn.setEnabled(False)
        self.pause_route_btn.setEnabled(True)
        self.resume_route_btn.setEnabled(False)
        self.stop_route_btn.setEnabled(True)
        self.show_route_btn.setEnabled(False)
        # self.joystick_action.setEnabled(False)
        self.view.page().runJavaScript("window.routeActive=true;")
        self.load_route_btn.setEnabled(False)
        self.route_timer.start(100)

    def advance_route(self):
        count = len(self.route_points)
        if count == 0:
            self.stop_route()
            return

        # If we are resuming to previous paused point
        if self.resuming_to_target:
            if self.resume_from_lat is None or self.resume_from_lon is None:
                self.resuming_to_target = False
                return  # Safely skip resume if no valid point was saved
            tlat = self.resume_from_lat
            tlon = self.resume_from_lon
        else:
            if not (0 <= self.route_index < count):
                self.stop_route()
                return
            tlat, tlon = self.route_points[self.route_index]

        # Compute movement
        lat0, lon0 = self.current_lat, self.current_lon
        lat_rad = math.radians(lat0)
        dlat = tlat - lat0
        dlon = tlon - lon0
        dlat_km = dlat * 111.32
        dlon_km = dlon * 111.32 * math.cos(lat_rad)
        dist = math.hypot(dlat_km, dlon_km)
        dt = self.route_timer.interval() / 1000.0
        step = self.speed * dt / 3600.0

        if dist <= step:
            self.update_position(tlat, tlon)

            if self.resuming_to_target:
                # Done resuming
                self.resuming_to_target = False
                self.resume_from_lat = None
                self.resume_from_lon = None
                return

            # Handle resume-to-target logic
            if getattr(self, "resuming_to_target", False):
                self.resuming_to_target = False
                self.resume_target = None
                return

            # Regular route logic
            if not self.loop_route_cb.isChecked():
                self.route_index += 1
                if self.route_index >= count:
                    self.stop_route()
                return
            # Looping logic
            if not self.loop_back:
                self.route_index += 1
                if self.route_index >= count:
                    self.loop_back = True
                    self.route_index = count - 2 if count > 1 else 0
            else:
                self.route_index -= 1
                if self.route_index < 0:
                    self.loop_back = False
                    self.route_index = 1 if count > 1 else 0
        else:
            ux = dlon_km / dist
            uy = dlat_km / dist
            new_lat = lat0 + (uy * step) / 111.32
            new_lon = lon0 + (ux * step) / (111.32 * math.cos(lat_rad))
            self.update_position(new_lat, new_lon)

    def pause_route(self):
        self.route_timer.stop()
        self.pause_route_btn.setEnabled(False)
        self.resume_route_btn.setEnabled(True)
        self.view.page().runJavaScript("window.routeActive=false;")
        self.load_route_btn.setEnabled(True)
        self.show_route_btn.setEnabled(True)

        # Only save resume point if it's close to any route point
        if self.is_position_on_route(self.current_lat, self.current_lon):
            self.resume_from_lat = self.current_lat
            self.resume_from_lon = self.current_lon
            self.resuming_to_target = True  # Enable returning to this point
        else:
            if self.is_position_on_route(self.resume_from_lat, self.resume_from_lon):
                return
            self.resume_from_lat = None
            self.resume_from_lon = None
            self.resuming_to_target = False  # Skip resuming if not near a route

    def is_position_on_route(self, lat, lon, tolerance_meters=0.5):
        if lat is None or lon is None or len(self.route_points) < 2:
            return False

        for i in range(len(self.route_points) - 1):
            lat1, lon1 = self.route_points[i]
            lat2, lon2 = self.route_points[i + 1]
            distance = self.point_to_segment_distance(lat, lon, lat1, lon1, lat2, lon2)
            if distance <= tolerance_meters:
                return True

        return False

    def point_to_segment_distance(self, lat, lon, lat1, lon1, lat2, lon2):
        """
        Calculates the perpendicular distance in meters from point (lat, lon)
        to the segment (lat1, lon1)-(lat2, lon2) using a planar projection.
        """
        from math import radians, cos, sin, sqrt

        R = 6371000  # Earth's radius in meters

        def latlon_to_xy(lat, lon):
            lat_rad = radians(lat)
            lon_rad = radians(lon)
            x = R * lon_rad * cos(radians((lat1 + lat2) / 2))
            y = R * lat_rad
            return x, y

        px, py = latlon_to_xy(lat, lon)
        x1, y1 = latlon_to_xy(lat1, lon1)
        x2, y2 = latlon_to_xy(lat2, lon2)

        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            # Segment is a single point
            return sqrt((px - x1) ** 2 + (py - y1) ** 2)

        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0, min(1, t))
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy

        return sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)

    def pause_route_if_active(self):
        if self.route_timer.isActive():
            self.pause_route()

    def resume_route(self):
        self.resuming_to_target = True
        self.resume_route_btn.setEnabled(False)
        self.pause_route_btn.setEnabled(True)
        self.view.page().runJavaScript("window.routeActive=true;")
        self.load_route_btn.setEnabled(False)
        self.show_route_btn.setEnabled(False)
        self.route_timer.start(100)

    def stop_route(self):
        self.route_timer.stop()
        self.resuming_to_target = False
        self.resume_from_lat = None
        self.resume_from_lon = None
        self.start_route_btn.setEnabled(True)
        self.pause_route_btn.setEnabled(False)
        self.resume_route_btn.setEnabled(False)
        self.stop_route_btn.setEnabled(False)
        self.joystick_action.setEnabled(True)
        self.view.page().runJavaScript("window.routeActive=false;")
        self.load_route_btn.setEnabled(True)
        self.show_route_btn.setEnabled(True)

    def save_route(self):
        if not self.route_points:
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Route", "", "Route Files (*.json)")
        if filename:
            if not filename.endswith(".json"):
                filename += ".json"
            try:
                with open(filename, 'w') as f:
                    json.dump([list(point) for point in self.route_points], f)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save route:\n{str(e)}")

    def load_route(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Route", "", "Route Files (*.json)")
        if filename:
            try:
                with open(filename, 'r') as f:
                    self.resuming_to_target = False
                    self.resume_from_lat = None
                    self.resume_from_lon = None
                    self.route_points.clear()
                    self.view.page().runJavaScript("enableRouteSelection();")
                    self.view.page().runJavaScript("disableRouteSelection();")
                    self.route_points = [tuple(pt) for pt in json.load(f)]
                    self.route_index = 0
                    self.start_route_btn.setEnabled(True)
                    self.loop_route_cb.setEnabled(True)
                    self.save_route_btn.setEnabled(True)
                    self.show_route_btn.setEnabled(True)
                    self.resume_route_btn.setEnabled(False)
                    self.stop_route_btn.setEnabled(False)
                    # Send to JS to draw route
                    self.view.page().runJavaScript("clearRouteLine();")
                    js_points = json.dumps(self.route_points)
                    self.view.page().runJavaScript(f"drawRouteFromPython({js_points});")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load route:\n{str(e)}")

    def teleport_to_route_start(self):
        if not self.route_points:
            return

        lat, lon = self.route_points[0]
        # Just center the map view, do not move the marker or GPS
        self.view.page().runJavaScript(f"map.setView([{lat}, {lon}], map.getZoom());")

    def clear_route(self):
        self.route_points.clear()

    def on_speed_changed(self, v):
        self.speed = v
        self.view.page().runJavaScript(f"walkSpeed = {v};")

    def on_joystick_toggled(self, checked):
        self.view.page().runJavaScript(f"toggleJoystick({str(checked).lower()});")

    def on_searchbar_toggled(self, checked):
        js = f"document.getElementById('searchBox').parentElement.style.display = {'\"block\"' if checked else '\"none\"'};"
        self.view.page().runJavaScript(js)

    def darkmode_toggled(self, enabled):
        mode = "true" if enabled else "false"
        self.view.page().runJavaScript(f"setDarkMode({mode});")

        if enabled:
            # Set native title bar to dark gray like rest of UI (#2c2c2c)
            self.set_titlebar_color("#2c2c2c")
        else:
            # Reset to system default (usually light or transparent)
            self.set_titlebar_color(None)

        dark_qss = """
            QMainWindow {
                background-color: #2c2c2c;
                color: #eee;
            }
            QMenuBar {
                background-color: #2c2c2c;
                color: #eee;
            }
            QMenuBar::item {
                background-color: #2c2c2c;
                color: #eee;
            }
            QMenuBar::item:selected {
                background-color: #444;
            }
            QMenu {
                background-color: #2c2c2c;
                color: #eee;
            }
            QMenu::item:selected {
                background-color: #444;
            }
            QMenu::item:disabled {
                color: #555;
                background-color: transparent;
            }
            QDockWidget, QWidget {
                background-color: #2c2c2c;
                color: #eee;
            }
            QLabel, QCheckBox {
                color: #eee;
            }
            QComboBox, QDoubleSpinBox, QPushButton {
                background-color: #444;
                color: #eee;
            }
            QPushButton:hover {
                background-color: #555;
            }
            QComboBox:disabled, QDoubleSpinBox:disabled, QPushButton:disabled {
                background-color: #333;
                color: #777;
            }
            QDockWidget::title {
                background-color: #3a3a3a;
                color: #eee;
                border: 1px solid white;
                text-align: left;
            }
        """

        light_qss = ""

        self.setStyleSheet(dark_qss if enabled else light_qss)

    def keyPressEvent(self, event):
        if self.route_timer.isActive(): return
        k = event.key()
        if k == QtCore.Qt.Key_Up: self.up_pressed = True
        elif k == QtCore.Qt.Key_Down: self.down_pressed = True
        elif k == QtCore.Qt.Key_Left: self.left_pressed = True
        elif k == QtCore.Qt.Key_Right: self.right_pressed = True
        else: super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.route_timer.isActive(): return
        k = event.key()
        if k == QtCore.Qt.Key_Up: self.up_pressed = False
        elif k == QtCore.Qt.Key_Down: self.down_pressed = False
        elif k == QtCore.Qt.Key_Left: self.left_pressed = False
        elif k == QtCore.Qt.Key_Right: self.right_pressed = False
        else: super().keyReleaseEvent(event)

    def eventFilter(self, obj, event):
        if self.route_timer.isActive(): return False
        if event.type() == QtCore.QEvent.KeyPress and event.key() in (
            QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
            QtCore.Qt.Key_Left, QtCore.Qt.Key_Right
        ):
            self.keyPressEvent(event)
            return True
        if event.type() == QtCore.QEvent.KeyRelease and event.key() in (
            QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
            QtCore.Qt.Key_Left, QtCore.Qt.Key_Right
        ):
            self.keyReleaseEvent(event)
            return True
        return super().eventFilter(obj, event)

    def handle_movement(self):
        if self.route_timer.isActive(): return
        if not any((self.up_pressed, self.down_pressed, self.left_pressed, self.right_pressed)): return
        dt = self.timer.interval() / 1000.0
        dist = self.speed * dt / 3600.0
        dlat = dist / 111.32
        dlon = dist / (111.32 * math.cos(math.radians(self.current_lat)))
        nl = self.current_lat + (dlat if self.up_pressed else -dlat if self.down_pressed else 0)
        nlng = self.current_lon + (dlon if self.right_pressed else -dlon if self.left_pressed else 0)
        self.update_position(nl, nlng)

    def update_position(self, lat, lon):
        self.current_lat, self.current_lon = lat, lon
        js = f"marker.setLatLng([{lat}, {lon}]); map.panTo([{lat}, {lon}]);"
        self.view.page().runJavaScript(js)
        self.send_location_to_selected_tunnel(lat, lon)

    def on_js_coordinates(self, lat, lon):
        self.update_position(lat, lon)

    def set_titlebar_color(self, color_hex=None):
        hwnd = int(self.winId())
        DWMWA_CAPTION_COLOR = 35
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20

        # Enable immersive dark mode toggle
        use_dark = ctypes.c_int(1 if color_hex else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(use_dark),
            ctypes.sizeof(use_dark)
        )

if __name__ == '__main__':
    # Start the tunnel server before anything else
    server_thread = threading.Thread(target=start_tunneld_server, daemon=True)
    server_thread.start()
    time.sleep(5)

    # UI
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("Assets/Icon.ico")))  # Taskbar icon (application-wide)
    w = MainWindow()
    w.showMaximized()
    sys.exit(app.exec_())