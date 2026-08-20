import 'dart:async';

import 'package:flutter/foundation.dart';

import '../domain/insole_device_state.dart';
import '../domain/insole_side.dart';

class InsoleManager extends ChangeNotifier {
  final List<InsoleDeviceState> _devices = <InsoleDeviceState>[];
  bool _isStreaming = false;

  List<InsoleDeviceState> get devices => List<InsoleDeviceState>.unmodifiable(_devices);
  bool get isStreaming => _isStreaming;

  InsoleDeviceState? get leftDevice {
    for (final InsoleDeviceState d in _devices) {
      if (d.side == InsoleSide.left) {
        return d;
      }
    }
    return null;
  }

  InsoleDeviceState? get rightDevice {
    for (final InsoleDeviceState d in _devices) {
      if (d.side == InsoleSide.right) {
        return d;
      }
    }
    return null;
  }

  void addMockDevice(String id, String name) {
    _devices.add(InsoleDeviceState(deviceId: id, name: name));
    notifyListeners();
  }

  void clearDevices() {
    _devices.clear();
    _isStreaming = false;
    notifyListeners();
  }

  void assignSide(String deviceId, InsoleSide side) {
    for (final InsoleDeviceState d in _devices) {
      if (d.deviceId == deviceId) {
        d.side = side;
      } else if (side != InsoleSide.unassigned && d.side == side) {
        d.side = InsoleSide.unassigned;
      }
    }
    notifyListeners();
  }

  Future<void> connectAll() async {
    for (final InsoleDeviceState d in _devices) {
      d.connected = true;
    }
    notifyListeners();
  }

  Future<void> startStreaming() async {
    _isStreaming = true;
    for (final InsoleDeviceState d in _devices) {
      if (d.connected && d.side != InsoleSide.unassigned) {
        d.streaming = true;
      }
    }
    notifyListeners();
  }

  Future<void> stopStreaming() async {
    _isStreaming = false;
    for (final InsoleDeviceState d in _devices) {
      d.streaming = false;
    }
    notifyListeners();
  }
}
