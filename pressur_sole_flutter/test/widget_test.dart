import 'package:flutter_test/flutter_test.dart';

import 'package:pressur_sole_flutter/app.dart';

void main() {
  testWidgets('App renders title', (WidgetTester tester) async {
    await tester.pumpWidget(const PressurSoleApp());

    expect(find.text('Pressur Sole Flutter'), findsOneWidget);
  });
}
