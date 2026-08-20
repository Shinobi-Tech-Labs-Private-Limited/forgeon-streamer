import 'package:flutter/material.dart';

import '../../insole/application/insole_manager.dart';

class MonitorScreen extends StatefulWidget {
  const MonitorScreen({super.key, required this.manager});

  final InsoleManager manager;

  @override
  State<MonitorScreen> createState() => _MonitorScreenState();
}

class _MonitorScreenState extends State<MonitorScreen> {
  @override
  void initState() {
    super.initState();
    widget.manager.addListener(_onManagerUpdate);
  }

  @override
  void dispose() {
    widget.manager.removeListener(_onManagerUpdate);
    super.dispose();
  }

  void _onManagerUpdate() {
    if (mounted) {
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    final left = widget.manager.leftDevice;
    final right = widget.manager.rightDevice;

    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Wrap(
            spacing: 8,
            children: <Widget>[
              FilledButton(
                onPressed: widget.manager.isStreaming ? widget.manager.stopStreaming : widget.manager.startStreaming,
                child: Text(widget.manager.isStreaming ? 'Stop Streaming' : 'Start Streaming'),
              ),
              const Chip(label: Text('Target: 200 Hz per device')),
            ],
          ),
          const SizedBox(height: 16),
          Expanded(
            child: Row(
              children: <Widget>[
                Expanded(child: _devicePanel(context, 'Left Device', left?.name ?? '--', left?.samplesPerSecond ?? 0)),
                const SizedBox(width: 12),
                Expanded(child: _devicePanel(context, 'Right Device', right?.name ?? '--', right?.samplesPerSecond ?? 0)),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _devicePanel(BuildContext context, String title, String name, double hz) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(name),
            const SizedBox(height: 8),
            Text('Rx rate: ${hz.toStringAsFixed(1)} Hz'),
            const SizedBox(height: 8),
            Text(
              hz >= 195 ? 'Rate status: PASS' : 'Rate status: Pending',
              style: TextStyle(
                color: hz >= 195 ? Colors.green : Colors.orange,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
