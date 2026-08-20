class BleUuids {
  static const String adcService = '2a90f079-8412-4953-951c-cb3e2d27c8d4';
  static const String adcChar = 'aa0a4d54-2b51-42f9-bbca-3b9304fbed92';

  static const String commandService = 'e4b7f8d1-3c19-4f7a-9c8a-f2d79371b44e';
  static const String commandChar = '7d4a93e2-1b7e-41c5-a2ed-8f0cf19e68e3';

  static const String statusService = 'a3f1c8b2-7d44-4e9f-b2a1-c8f37d9a12ef';
  static const String statusChar = '7d4a93e2-1b22-4a61-95b4-564f0a2c7703';

  static const String disService = '0000180a-0000-1000-8000-00805f9b34fb';
  static const String modelNumber = '00002a24-0000-1000-8000-00805f9b34fb';
  static const String manufacturerName = '00002a29-0000-1000-8000-00805f9b34fb';
  static const String firmwareRevision = '00002a26-0000-1000-8000-00805f9b34fb';
  static const String hardwareRevision = '00002a27-0000-1000-8000-00805f9b34fb';
}

class InsoleCommands {
  static const List<int> ledToggle = [0x01];
  static const List<int> freq10Hz = [0x0A];
  static const List<int> freq100Hz = [0x0B];
  static const List<int> freq200Hz = [0x0C];
}
