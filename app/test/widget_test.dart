// Smoke test: the app builds and shows its title.
import 'package:flutter_test/flutter_test.dart';
import 'package:netinventory/main.dart';

void main() {
  testWidgets('app renders with title', (WidgetTester tester) async {
    await tester.pumpWidget(const NetInvApp());
    expect(find.text('netinventory'), findsWidgets);
  });
}
