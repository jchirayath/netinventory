// Unit tests for the Batteries-tab view logic, and a render of the tab.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:netinventory/api.dart';
import 'package:netinventory/batteries.dart';
import 'package:netinventory/theme.dart';

void main() {
  final devices = [
    {'device': 'Flume Sensor', 'pct': 10, 'label': 'low', 'battery': '2x AA', 'next_due': '2026-01-01'},
    {'device': 'Front Door Lock', 'pct': 45, 'label': '', 'battery': '4x AA', 'next_due': ''},
    {'device': 'H5101_4670', 'pct': null, 'battery': ''},
  ];

  test('colour follows the level bands, grey with no reading', () {
    expect(batteryColor(5), Brand.weak);
    expect(batteryColor(20), Brand.weak);
    expect(batteryColor(50), Brand.fair);
    expect(batteryColor(51), Brand.good);
    expect(batteryColor(null), Brand.offline);
  });

  test('level text shows a differing label only', () {
    expect(batteryLevelText(devices[0]), '10% (low)');
    expect(batteryLevelText(devices[1]), '45%');
    expect(batteryLevelText({'pct': 100, 'label': '100'}), '100%');
    expect(batteryLevelText(devices[2]), 'no reading');
  });

  test('filter matches device and battery type, keeps order', () {
    expect(filterBatteries(devices, '').length, 3);
    expect(filterBatteries(devices, 'aa').map((d) => d['device']),
        ['Flume Sensor', 'Front Door Lock']);
    expect(filterBatteries(devices, 'LOCK').single['device'], 'Front Door Lock');
  });

  test('due on or after the expected date; blank never due', () {
    expect(batteryDue(devices[0], '2026-01-01'), isTrue);
    expect(batteryDue(devices[0], '2025-12-31'), isFalse);
    expect(batteryDue(devices[1], '2099-01-01'), isFalse);
  });

  test('low count ignores devices without a reading', () {
    expect(lowBatteryCount(devices), 1);
  });

  test('isoDate pads', () {
    expect(isoDate(DateTime(2026, 3, 7)), '2026-03-07');
  });

  testWidgets('tab renders rows, search, and a device sheet', (tester) async {
    tester.view.physicalSize = const Size(390 * 3, 844 * 3); // phone width
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
      theme: buildTheme(Brightness.dark),
      home: Scaffold(
        body: BatteriesTab(
          api: Api('http://127.0.0.1:1'),
          data: {
            'devices': devices,
            'log': [
              {'Id': 'a1', 'Date': '2026-11-11', 'Device': 'Flume Sensor', 'Event': 'replaced',
               'Battery': '2x AA', 'Level': '', 'Notes': 'Energizer', 'Source': 'manual'}
            ],
            'events': ['replaced', 'charged', 'checked', 'note'],
          },
          onChanged: () async {},
        ),
      ),
    ));
    expect(find.text('Flume Sensor'), findsOneWidget);
    expect(find.text('10% (low)'), findsOneWidget);
    expect(find.text('no reading'), findsOneWidget);
    await tester.enterText(find.byType(TextField).first, 'lock');
    await tester.pump();
    expect(find.text('Flume Sensor'), findsNothing);
    await tester.enterText(find.byType(TextField).first, '');
    await tester.pump();
    await tester.tap(find.text('Flume Sensor'));
    await tester.pumpAndSettle();
    expect(find.text('Change log'), findsOneWidget);
    expect(find.textContaining('Energizer'), findsOneWidget);
  });

  test('write result flags', () {
    expect(const WriteResult(401, {}).needsPasscode, isTrue);
    expect(const WriteResult(200, {}).ok, isTrue);
    expect(const WriteResult(400, {'error': 'device is required'}).error,
        'device is required');
    expect(const WriteResult(0, {}).error, 'HTTP 0');
  });
}
