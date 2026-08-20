import 'dart:typed_data';

class StatusPacket {
  StatusPacket({
    required this.isCharging,
    required this.isStreaming,
    required this.batteryVoltage,
    required this.frequencyCode,
  });

  final bool isCharging;
  final bool isStreaming;
  final int batteryVoltage;
  final int frequencyCode;
}

class AdcPacket {
  AdcPacket({
    required this.deviceTimestampMs,
    required this.channels,
  });

  final int deviceTimestampMs;
  final List<int> channels;
}

class InsolePacketParser {
  static StatusPacket? parseStatus(List<int> raw) {
    if (raw.length < 8) {
      return null;
    }

    final data = Uint8List.fromList(raw);
    final bytes = ByteData.sublistView(data);

    return StatusPacket(
      isCharging: data[0] != 0,
      isStreaming: data[1] != 0,
      batteryVoltage: bytes.getUint16(2, Endian.little),
      frequencyCode: data[4],
    );
  }

  static AdcPacket? parseAdc(List<int> raw) {
    if (raw.length != 20) {
      return null;
    }

    final data = Uint8List.fromList(raw);
    final bytes = ByteData.sublistView(data);
    final channels = List<int>.generate(
      8,
      (int i) => bytes.getUint16(2 + (i * 2), Endian.little),
      growable: false,
    );

    return AdcPacket(
      deviceTimestampMs: bytes.getUint16(0, Endian.little),
      channels: channels,
    );
  }
}
