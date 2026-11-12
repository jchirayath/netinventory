// Live test of the app's API client against a running agent. Skipped unless
// NETINV_E2E is set to the agent's LAN address (e.g. http://192.168.1.5:8787)
// and NETINV_E2E_PASS to its passcode; the agent must run
// `netinv serve --lan --allow-write` on a throwaway copy of the data.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:netinventory/api.dart';

void main() {
  final base = Platform.environment['NETINV_E2E'] ?? '';
  final pass = Platform.environment['NETINV_E2E_PASS'] ?? '';
  final skip = base.isEmpty ? 'set NETINV_E2E to run against a live agent' : null;

  setUpAll(() => HttpOverrides.global = null); // flutter_test blocks real HTTP

  test('reads batteries, needs the passcode to write, then edits and deletes', () async {
    final api = Api(base);
    final data = await api.batteries();
    expect((data['devices'] as List).isNotEmpty, isTrue);

    final entry = {'device': 'Flume Sensor', 'event': 'checked', 'notes': 'app e2e'};
    final denied = await api.logBattery(entry);
    expect(denied.needsPasscode, isTrue, reason: 'status ${denied.status}');

    api.passcode = pass;
    final ok = await api.logBattery(entry);
    expect(ok.ok, isTrue, reason: ok.error);
    final id = '${(ok.body['entry'] as Map)['Id']}';

    expect((await api.editBattery(id, {'notes': 'app e2e edited'})).ok, isTrue);
    final one = await api.battery('Flume Sensor');
    expect((one['log'] as List).any((e) => e['Notes'] == 'app e2e edited'), isTrue);

    final bad = await api.logBattery({'device': ''});
    expect(bad.status, 400);
    expect(bad.error, contains('device'));

    expect((await api.deleteBattery(id)).ok, isTrue);
    final after = await api.batteries();
    expect((after['log'] as List).any((e) => e['Id'] == id), isFalse);
  }, skip: skip);
}
