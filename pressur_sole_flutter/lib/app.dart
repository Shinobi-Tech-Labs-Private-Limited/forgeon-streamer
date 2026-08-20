import 'package:flutter/material.dart';

import 'features/insole/application/insole_manager.dart';
import 'features/ui/screens/connection_screen.dart';
import 'features/ui/screens/monitor_screen.dart';

class PressurSoleApp extends StatefulWidget {
  const PressurSoleApp({super.key});

  @override
  State<PressurSoleApp> createState() => _PressurSoleAppState();
}

class _PressurSoleAppState extends State<PressurSoleApp> {
  final InsoleManager manager = InsoleManager();

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Pressur Sole Flutter',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF00897B)),
        useMaterial3: true,
      ),
      home: DefaultTabController(
        length: 2,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Pressur Sole Flutter'),
            bottom: const TabBar(
              tabs: [
                Tab(text: 'Connection'),
                Tab(text: 'Monitor & Stream'),
              ],
            ),
          ),
          body: TabBarView(
            children: [
              ConnectionScreen(manager: manager),
              MonitorScreen(manager: manager),
            ],
          ),
        ),
      ),
    );
  }
}
