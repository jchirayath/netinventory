// Unit tests for the Devices-tab view logic (filter / sort / group).
import 'package:flutter_test/flutter_test.dart';
import 'package:netinventory/main.dart';

Map dev(String ip, String status, String band, String signal, int? rssi,
        {String device = '', String host = '', String cat = 'Other'}) =>
    {
      'IP': ip,
      'MAC': '00:00:00:00:00:0${ip.split('.').last[0]}',
      'Device': device,
      'Hostname': host,
      'category': cat,
      'band': band,
      'signal': signal,
      'rssi': rssi,
      'Status': status,
      'Vendor': 'Acme',
    };

void main() {
  final sample = [
    dev('192.168.1.10', 'online', '5 GHz', 'weak', -80, device: 'Windows PC'),
    dev('192.168.1.2', 'online', '2.4 GHz', 'good', -55, device: 'Plug'),
    dev('192.168.1.100', 'offline', '5 GHz', 'fair', -70, host: 'iPhone'),
    dev('192.168.1.5', 'online', 'Ethernet', '', null, device: 'NAS'),
    dev('192.168.1.9', 'defunct', '2.4 GHz', 'weak', -85, device: 'OldCam'),
  ];

  test('IP sort is numeric, not lexical', () {
    final ips =
        viewRows(sample, sortKey: 'IP').map((d) => d['IP']).toList();
    expect(ips, ['192.168.1.2', '192.168.1.5', '192.168.1.9', '192.168.1.10',
        '192.168.1.100']);
  });

  test('signal sort puts weakest first and wired (null rssi) last', () {
    final rows = viewRows(sample, sortKey: 'signal', sortAsc: true);
    expect(rows.first['rssi'], -85); // weakest
    expect(rows.last['rssi'], isNull); // wired sorts last regardless
  });

  test('onlyWeak keeps just weak-signal devices', () {
    final rows = viewRows(sample, onlyWeak: true);
    expect(rows.every((d) => d['signal'] == 'weak'), isTrue);
    expect(rows.length, 2);
  });

  test('onlyUnnamed keeps devices with no friendly Device name', () {
    final rows = viewRows(sample, onlyUnnamed: true);
    // only the iPhone entry (host-only, empty Device) qualifies
    expect(rows.length, 1);
    expect(rows.every((d) => '${d['Device']}'.trim().isEmpty), isTrue);
  });

  test('hideDefunct drops defunct rows', () {
    final rows = viewRows(sample, hideDefunct: true);
    expect(rows.any((d) => d['Status'] == 'defunct'), isFalse);
  });

  test('global filter searches all fields', () {
    final rows = viewRows(sample, filter: 'iphone');
    expect(rows.length, 1);
    expect(rows.first['Hostname'], 'iPhone');
  });

  test('group by signal yields ordered headers with roll-ups', () {
    final rows = viewRows(sample, groupBy: 'signal');
    final headers = rows
        .where((r) => r is Map && r['__grp'] != null)
        .map((r) => r['__grp'])
        .toList();
    // fixed order good < fair < weak < wired
    expect(headers,
        ['Good signal', 'Fair signal', 'Weak signal', 'Wired / no signal']);
    // each header carries its device list for roll-up rendering
    final weak = rows.firstWhere((r) => r is Map && r['__grp'] == 'Weak signal');
    expect((weak['items'] as List).length, 2);
  });

  test('collapsed group hides its device rows but keeps the header', () {
    final rows = viewRows(sample,
        groupBy: 'signal', collapsed: {'signal:Weak signal'});
    final hasWeakHeader =
        rows.any((r) => r is Map && r['__grp'] == 'Weak signal');
    final hasWeakDevice = rows.any((r) => r is Map && r['signal'] == 'weak');
    expect(hasWeakHeader, isTrue);
    expect(hasWeakDevice, isFalse);
  });

  test('deviceName falls back Device -> Hostname -> (unnamed)', () {
    expect(deviceName({'Device': 'PC', 'Hostname': 'h'}), 'PC');
    expect(deviceName({'Device': '', 'Hostname': 'h'}), 'h');
    expect(deviceName({'Device': '', 'Hostname': ''}), '(unnamed)');
  });
}
