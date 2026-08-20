import 'dart:convert';
import 'dart:io';

class JsonLogger {
  Future<File> writeSession({
    required String path,
    required Map<String, dynamic> payload,
  }) async {
    final File file = File(path);
    await file.writeAsString(const JsonEncoder.withIndent('  ').convert(payload));
    return file;
  }
}
