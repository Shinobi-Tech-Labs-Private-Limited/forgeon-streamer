import 'package:flutter/material.dart';

import '../../insole/application/insole_manager.dart';
import '../../insole/domain/insole_side.dart';

class ConnectionScreen extends StatefulWidget {
  const ConnectionScreen({super.key, required this.manager});

  final InsoleManager manager;

  @override
  State<ConnectionScreen> createState() => _ConnectionScreenState();
}

class _ConnectionScreenState extends State<ConnectionScreen> {
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
    final devices = widget.manager.devices;

    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Wrap(
            spacing: 8,
            children: <Widget>[
              FilledButton(
                onPressed: () {
                  final now = DateTime.now().millisecondsSinceEpoch;
                  widget.manager.addMockDevice('L-$now', 'GDPS Left Candidate');
                },
                child: const Text('Add Mock Device'),
              ),
              OutlinedButton(
                onPressed: widget.manager.connectAll,
                child: const Text('Connect All'),
              ),
              OutlinedButton(
                onPressed: widget.manager.clearDevices,
                child: const Text('Clear'),
              ),
            ],
          ),
          const SizedBox(height: 16),
          const Text('Device Pool', style: TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          Expanded(
            child: ListView.separated(
              itemCount: devices.length,
              separatorBuilder: (BuildContext context, int index) => const SizedBox(height: 8),
              itemBuilder: (BuildContext context, int index) {
                final d = devices[index];
                return Card(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Row(
                      children: <Widget>[
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: <Widget>[
                              Text(d.name, style: const TextStyle(fontWeight: FontWeight.w600)),
                              Text(d.deviceId, style: Theme.of(context).textTheme.bodySmall),
                              Text(d.connected ? 'Connected' : 'Not connected'),
                            ],
                          ),
                        ),
                        DropdownButton<InsoleSide>(
                          value: d.side,
                          onChanged: (InsoleSide? side) {
                            if (side != null) {
                              widget.manager.assignSide(d.deviceId, side);
                            }
                          },
                          items: const <DropdownMenuItem<InsoleSide>>[
                            DropdownMenuItem(value: InsoleSide.unassigned, child: Text('Unassigned')),
                            DropdownMenuItem(value: InsoleSide.left, child: Text('Left')),
                            DropdownMenuItem(value: InsoleSide.right, child: Text('Right')),
                          ],
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}
