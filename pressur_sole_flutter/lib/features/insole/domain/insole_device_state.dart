import 'insole_side.dart';

class InsoleDeviceState {
  InsoleDeviceState({
    required this.deviceId,
    required this.name,
    this.side = InsoleSide.unassigned,
    this.connected = false,
    this.streaming = false,
    this.charging = false,
    this.batteryVoltage = 0,
    this.frequencyCode = 0x0C,
    List<int>? channels,
    this.packetCount = 0,
    this.samplesPerSecond = 0,
    this.modelNumber = '--',
    this.manufacturerName = '--',
    this.firmwareRevision = '--',
    this.hardwareRevision = '--',
  }) : channels = channels ?? List<int>.filled(8, 0);

  final String deviceId;
  final String name;
  InsoleSide side;
  bool connected;
  bool streaming;
  bool charging;
  int batteryVoltage;
  int frequencyCode;
  List<int> channels;
  int packetCount;
  double samplesPerSecond;
  String modelNumber;
  String manufacturerName;
  String firmwareRevision;
  String hardwareRevision;
}
