// netinventory — macOS/iOS companion. Reads the read-only JSON API served by
// `netinv serve` (default http://127.0.0.1:8787). On iPhone, point Host at the
// Mac agent's LAN IP.
import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'theme.dart';
import 'widgets.dart';

void main() => runApp(const NetInvApp());

// --- Pure device-table view logic (group / sort / filter) -----------------
// Extracted as top-level functions so they can be unit-tested without a
// running app or network. The Devices tab delegates to these.

String deviceName(Map d) => (d['Device'] as String?)?.isNotEmpty == true
    ? d['Device']
    : (d['Hostname'] as String?)?.isNotEmpty == true
        ? d['Hostname']
        : '(unnamed)';

int ipNum(String ip) =>
    ip.split('.').fold(0, (a, p) => a * 256 + (int.tryParse(p) ?? 0));

bool passDevice(Map d,
    {String filter = '',
    bool onlyWeak = false,
    bool onlyOffline = false,
    bool onlyUnnamed = false,
    bool hideDefunct = false}) {
  final q = filter.toLowerCase();
  if (q.isNotEmpty && !jsonEncode(d).toLowerCase().contains(q)) return false;
  if (onlyWeak && d['signal'] != 'weak') return false;
  if (onlyOffline && d['Status'] != 'offline') return false;
  if (onlyUnnamed && '${d['Device'] ?? ''}'.trim().isNotEmpty) return false;
  if (hideDefunct && d['Status'] == 'defunct') return false;
  return true;
}

int cmpDevice(Map a, Map b, String sortKey, bool sortAsc) {
  int r;
  switch (sortKey) {
    case 'Name':
      r = deviceName(a).toLowerCase().compareTo(deviceName(b).toLowerCase());
      break;
    case 'signal':
      final x = a['rssi'], y = b['rssi']; // nulls (wired) always sort last
      if (x == null && y == null) {
        r = 0;
      } else if (x == null) {
        return 1;
      } else if (y == null) {
        return -1;
      } else {
        r = (x as int).compareTo(y as int);
      }
      break;
    case 'Status':
      r = '${a['Status']}'.compareTo('${b['Status']}');
      break;
    default: // IP
      r = ipNum('${a['IP']}').compareTo(ipNum('${b['IP']}'));
  }
  return sortAsc ? r : -r;
}

String grpKey(Map d, String groupBy) {
  switch (groupBy) {
    case 'band':
      return (d['band'] as String?)?.isNotEmpty == true ? d['band'] : '(none)';
    case 'category':
      return (d['category'] as String?)?.isNotEmpty == true
          ? d['category']
          : '(uncategorized)';
    case 'Status':
      return '${d['Status']}';
    case 'signal':
      return const {
            'good': 'Good signal',
            'fair': 'Fair signal',
            'weak': 'Weak signal'
          }['${d['signal']}'] ??
          'Wired / no signal';
    default:
      return '';
  }
}

int grpOrder(String label, String groupBy) => groupBy == 'signal'
    ? const {
          'Good signal': 0,
          'Fair signal': 1,
          'Weak signal': 2,
          'Wired / no signal': 3
        }[label] ??
        9
    : 0;

/// Flattened rows: group-header maps ({'__grp':label,'items':[...]}) interleaved
/// with device maps (respecting [collapsed]); or just sorted devices when
/// [groupBy] is empty.
List viewRows(List devices,
    {String groupBy = '',
    String sortKey = 'IP',
    bool sortAsc = true,
    String filter = '',
    bool onlyWeak = false,
    bool onlyOffline = false,
    bool onlyUnnamed = false,
    bool hideDefunct = false,
    Set<String> collapsed = const {}}) {
  final list = devices
      .map((d) => d as Map)
      .where((d) => passDevice(d,
          filter: filter,
          onlyWeak: onlyWeak,
          onlyOffline: onlyOffline,
          onlyUnnamed: onlyUnnamed,
          hideDefunct: hideDefunct))
      .toList()
    ..sort((a, b) => cmpDevice(a, b, sortKey, sortAsc));
  if (groupBy.isEmpty) return list;
  final groups = <String, List<Map>>{};
  for (final d in list) {
    groups.putIfAbsent(grpKey(d, groupBy), () => []).add(d);
  }
  final keys = groups.keys.toList()
    ..sort((a, b) => groupBy == 'signal'
        ? grpOrder(a, groupBy).compareTo(grpOrder(b, groupBy))
        : a.toLowerCase().compareTo(b.toLowerCase()));
  final rows = [];
  for (final k in keys) {
    rows.add({'__grp': k, 'items': groups[k]!});
    if (!collapsed.contains('$groupBy:$k')) rows.addAll(groups[k]!);
  }
  return rows;
}

class NetInvApp extends StatelessWidget {
  const NetInvApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'netinventory',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(Brightness.light),
      darkTheme: buildTheme(Brightness.dark),
      themeMode: ThemeMode.dark,
      home: const HomePage(),
    );
  }
}

class Api {
  String base;
  Api(this.base);

  Future<dynamic> _get(String path) async {
    final r = await http
        .get(Uri.parse('$base$path'))
        .timeout(const Duration(seconds: 6));
    if (r.statusCode != 200) {
      throw 'HTTP ${r.statusCode}';
    }
    return jsonDecode(r.body);
  }

  Future<Map<String, dynamic>> summary() async =>
      (await _get('/api/summary')) as Map<String, dynamic>;
  Future<List<dynamic>> devices() async =>
      (await _get('/api/devices')) as List<dynamic>;
  Future<List<dynamic>> alerts() async =>
      (await _get('/api/alerts')) as List<dynamic>;

  /// Rename a device. Returns the HTTP status (200 ok, 403 read-only,
  /// 404 unknown MAC), or 0 if the request failed to reach the agent.
  Future<int> setName(String mac, String name) async {
    try {
      final r = await http
          .post(Uri.parse('$base/api/name'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'mac': mac, 'name': name}))
          .timeout(const Duration(seconds: 6));
      return r.statusCode;
    } catch (_) {
      return 0;
    }
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  Api _api = Api('http://127.0.0.1:8787');
  Map<String, dynamic> _summary = {};
  List<dynamic> _devices = [];
  List<dynamic> _alerts = [];
  String _filter = '';
  String? _error;
  bool _loading = false;
  int _tab = 0;
  Timer? _timer;

  // Devices-table view state (group / sort / quick filters)
  String _groupBy = ''; // '', band, category, Status, signal
  String _sortKey = 'IP'; // IP, Name, signal, Status
  bool _sortAsc = true;
  bool _onlyWeak = false,
      _onlyOffline = false,
      _onlyUnnamed = false,
      _hideDefunct = false;
  final Set<String> _collapsed = {};

  @override
  void initState() {
    super.initState();
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 15), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    setState(() => _loading = true);
    try {
      final s = await _api.summary();
      final d = await _api.devices();
      final a = await _api.alerts();
      setState(() {
        _summary = s;
        _devices = d;
        _alerts = a;
        _error = null;
      });
    } catch (e) {
      setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _editHost() async {
    final ctrl = TextEditingController(text: _api.base);
    final v = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Agent host'),
        content: TextField(
          controller: ctrl,
          decoration: const InputDecoration(
              helperText:
                  'e.g. http://127.0.0.1:8787 (Mac) or the Mac\'s LAN IP'),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(c, ctrl.text.trim()),
              child: const Text('Save')),
        ],
      ),
    );
    if (v != null && v.isNotEmpty) {
      setState(() => _api = Api(v));
      _refresh();
    }
  }

  int get _weakCount =>
      _devices.where((d) => (d as Map)['signal'] == 'weak').length;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(children: [
          _header(),
          if (_error == null) _statStrip(),
          const Divider(),
          Expanded(
            child: _error != null
                ? _errorView()
                : _tab == 0
                    ? _deviceList()
                    : _alertList(),
          ),
        ]),
      ),
      bottomNavigationBar: _bottomBar(),
    );
  }

  // ── chrome ────────────────────────────────────────────────────────────────

  Widget _header() {
    final cs = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 10, 10, 8),
      child: Row(children: [
        const BrandMark(size: 30),
        const SizedBox(width: 10),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('netinventory',
                style: TextStyle(
                    fontSize: 16.5,
                    height: 1.1,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -.2)),
            Text('home network inventory',
                style: TextStyle(
                    fontSize: 11,
                    height: 1.2,
                    color: cs.onSurface.withValues(alpha: .5))),
          ],
        ),
        const Spacer(),
        _hostChip(),
        const SizedBox(width: 6),
        IconButton(
          onPressed: _loading ? null : _refresh,
          tooltip: 'Refresh',
          visualDensity: VisualDensity.compact,
          icon: _loading
              ? const SizedBox(
                  width: 15,
                  height: 15,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.refresh_rounded, size: 19),
        ),
      ]),
    );
  }

  Widget _hostChip() {
    final cs = Theme.of(context).colorScheme;
    final host = _api.base.replaceFirst(RegExp(r'^https?://'), '');
    return InkWell(
      onTap: _editHost,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
        decoration: BoxDecoration(
          color: cs.surface,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: Brand.line),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: _error == null ? Brand.good : Brand.weak),
          ),
          const SizedBox(width: 7),
          Text(host,
              style: TextStyle(
                  fontSize: 11.5, color: cs.onSurface.withValues(alpha: .75))),
        ]),
      ),
    );
  }

  Widget _statStrip() {
    final s = _summary;
    if (s.isEmpty) return const SizedBox(height: 4);
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 0, 14, 10),
      child: Row(children: [
        StatPill(
            value: '${s['total'] ?? 0}',
            label: 'devices',
            color: Brand.teal,
            icon: Icons.lan_rounded),
        const SizedBox(width: 8),
        StatPill(
            value: '${s['online'] ?? 0}',
            label: 'online',
            color: Brand.good,
            icon: Icons.circle),
        const SizedBox(width: 8),
        StatPill(
            value: '$_weakCount',
            label: 'weak',
            color: Brand.weak,
            icon: Icons.wifi_rounded),
        const SizedBox(width: 8),
        StatPill(
            value: '${s['unnamed'] ?? 0}',
            label: 'unnamed',
            color: Brand.fair,
            icon: Icons.help_outline_rounded),
      ]),
    );
  }

  Widget _bottomBar() {
    final cs = Theme.of(context).colorScheme;
    final high = _alerts.where((a) => a['severity'] == 'high').length;
    Widget item(int i, IconData icon, String label, {int badge = 0}) {
      final on = _tab == i;
      return Expanded(
        child: InkWell(
          onTap: () => setState(() => _tab = i),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 9),
            child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              Icon(icon,
                  size: 18,
                  color: on ? cs.primary : cs.onSurface.withValues(alpha: .5)),
              const SizedBox(width: 7),
              Text(label,
                  style: TextStyle(
                      fontSize: 12.5,
                      fontWeight: on ? FontWeight.w600 : FontWeight.w400,
                      color: on ? cs.primary : cs.onSurface.withValues(alpha: .6))),
              if (badge > 0) ...[
                const SizedBox(width: 6),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(
                      color: Brand.weak, borderRadius: BorderRadius.circular(20)),
                  child: Text('$badge',
                      style: const TextStyle(
                          fontSize: 10,
                          color: Colors.white,
                          fontWeight: FontWeight.w700)),
                ),
              ],
            ]),
          ),
        ),
      );
    }

    return Container(
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: Brand.line)),
        color: cs.surface,
      ),
      child: SafeArea(
        top: false,
        child: Row(children: [
          item(0, Icons.lan_rounded, 'Devices'),
          item(1, Icons.notifications_rounded, 'Alerts', badge: high),
        ]),
      ),
    );
  }

  Widget _errorView() => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Icon(Icons.cloud_off_rounded, size: 36, color: Brand.offline),
            const SizedBox(height: 12),
            Text('Cannot reach the agent\n$_error',
                textAlign: TextAlign.center),
            const SizedBox(height: 8),
            Text(
                'Run `netinv serve` on your Mac, then set the host in the header.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Brand.offline)),
            const SizedBox(height: 14),
            FilledButton(onPressed: _refresh, child: const Text('Retry')),
          ]),
        ),
      );

  // ── signal helpers ────────────────────────────────────────────────────────

  Color _signalColor(String sig) => sig == 'good'
      ? Brand.good
      : (sig == 'fair' ? Brand.fair : Brand.weak);

  String _signalReading(String sig) => sig == 'good'
      ? 'Good — solid link'
      : (sig == 'fair'
          ? 'Fair — usable but can drop under load'
          : 'Weak — likely cause of drops & failed DHCP');

  // ── device detail ─────────────────────────────────────────────────────────

  void _showDevice(Map<String, dynamic> d) {
    final name = deviceName(d);
    final rssi = d['rssi'];
    final sig = '${d['signal']}';
    final ctrl = TextEditingController(text: '${d['Device'] ?? ''}');
    showDialog(
      context: context,
      builder: (c) => AlertDialog(
        title: Row(children: [
          CategoryBadge(category: '${d['category']}'),
          const SizedBox(width: 10),
          Expanded(
              child: Text(name, style: const TextStyle(fontSize: 17))),
        ]),
        content: SizedBox(
          width: 340,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _kv('IP', '${d['IP']}'),
              _kv('MAC', '${d['MAC']}'),
              _kv('Vendor', '${d['Vendor']}'),
              _kv('Category', '${d['category']}'),
              _kv('Link', '${d['band']}'),
              _kv('Status', '${d['Status']}'),
              const Divider(height: 20),
              if (rssi != null) ...[
                Row(children: [
                  SignalBars(signal: sig, rssi: rssi as int),
                  const SizedBox(width: 8),
                  Text('dBm',
                      style: TextStyle(
                          fontSize: 12, color: _signalColor(sig))),
                ]),
                const SizedBox(height: 5),
                Text(_signalReading(sig),
                    style:
                        TextStyle(fontSize: 12.5, color: _signalColor(sig))),
              ] else
                Text('No Wi-Fi signal reported (wired, or off the gateway).',
                    style: TextStyle(fontSize: 12.5, color: Brand.offline)),
              const Divider(height: 20),
              TextField(
                controller: ctrl,
                decoration: const InputDecoration(
                  labelText: 'Device name',
                  hintText: 'e.g. Living Room TV',
                ),
                onSubmitted: (v) => _rename(c, d, v),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(c), child: const Text('Close')),
          FilledButton(
              onPressed: () => _rename(c, d, ctrl.text),
              child: const Text('Save name')),
        ],
      ),
    );
  }

  Future<void> _rename(BuildContext dialogCtx, Map<String, dynamic> d,
      String name) async {
    Navigator.pop(dialogCtx);
    final code = await _api.setName('${d['MAC']}', name.trim());
    if (!mounted) return;
    String msg;
    if (code == 200) {
      setState(() => d['Device'] = name.trim()); // optimistic
      _refresh(); // authoritative refresh (also updates summary/unnamed count)
      msg = name.trim().isEmpty ? 'Name cleared' : 'Renamed to "${name.trim()}"';
    } else if (code == 403) {
      msg = 'Agent is read-only — restart it with: netinv serve --allow-write';
    } else if (code == 0) {
      msg = 'Could not reach the agent';
    } else {
      msg = 'Rename failed (HTTP $code)';
    }
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg)));
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(
              width: 76,
              child: Text(k,
                  style: TextStyle(color: Brand.offline, fontSize: 12.5))),
          Expanded(child: Text(v.isEmpty ? '—' : v,
              style: const TextStyle(fontSize: 13))),
        ]),
      );

  // ── devices tab ───────────────────────────────────────────────────────────

  Widget _deviceList() {
    final rows = viewRows(_devices,
        groupBy: _groupBy,
        sortKey: _sortKey,
        sortAsc: _sortAsc,
        filter: _filter,
        onlyWeak: _onlyWeak,
        onlyOffline: _onlyOffline,
        onlyUnnamed: _onlyUnnamed,
        hideDefunct: _hideDefunct,
        collapsed: _collapsed);
    return Column(children: [
      _controlBar(),
      const Divider(),
      Expanded(
        child: rows.isEmpty
            ? Center(
                child: Text('No devices match those filters',
                    style: TextStyle(color: Brand.offline, fontSize: 13)))
            : RefreshIndicator(
                onRefresh: _refresh,
                child: ListView.separated(
                  padding: EdgeInsets.zero,
                  itemCount: rows.length,
                  separatorBuilder: (c, i) => const Divider(),
                  itemBuilder: (c, i) {
                    final row = rows[i];
                    if (row is Map && row['__grp'] != null) {
                      return _groupHeader(row['__grp'] as String,
                          (row['items'] as List).cast<Map>());
                    }
                    return _deviceTile(row as Map<String, dynamic>);
                  },
                ),
              ),
      ),
    ]);
  }

  Widget _deviceTile(Map<String, dynamic> d) {
    final cs = Theme.of(context).colorScheme;
    final offline = d['Status'] != 'online';
    final unnamed = '${d['Device'] ?? ''}'.trim().isEmpty;
    final weak = d['signal'] == 'weak';
    return InkWell(
      onTap: () => _showDevice(d),
      child: Container(
        height: Brand.rowH,
        padding: const EdgeInsets.symmetric(horizontal: 14),
        decoration: BoxDecoration(
          border: weak
              ? Border(left: BorderSide(color: Brand.weak, width: 2.5))
              : null,
        ),
        child: Row(children: [
          CategoryBadge(category: '${d['category']}', dim: offline),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(children: [
                  Flexible(
                    child: Text(deviceName(d),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            fontSize: 13.5,
                            height: 1.2,
                            fontWeight: FontWeight.w600,
                            color: offline
                                ? cs.onSurface.withValues(alpha: .45)
                                : cs.onSurface)),
                  ),
                  if (unnamed) ...[
                    const SizedBox(width: 6),
                    _tag('unnamed', Brand.fair),
                  ],
                  if (offline) ...[
                    const SizedBox(width: 6),
                    _tag('${d['Status']}', Brand.offline),
                  ],
                ]),
                const SizedBox(height: 2),
                Text(
                    [
                      '${d['IP']}'.isEmpty ? '—' : '${d['IP']}',
                      '${d['MAC']}',
                      if ('${d['Vendor']}'.isNotEmpty) '${d['Vendor']}',
                    ].join('  ·  '),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                        fontSize: 11.5,
                        height: 1.2,
                        color: cs.onSurface.withValues(alpha: .45),
                        fontFeatures: const [FontFeature.tabularFigures()])),
              ],
            ),
          ),
          const SizedBox(width: 10),
          SizedBox(
            width: 92,
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                SignalBars(
                    signal: '${d['signal']}', rssi: d['rssi'] as int?),
                const SizedBox(height: 3),
                Text('${d['band']}',
                    style: TextStyle(
                        fontSize: 11,
                        height: 1.1,
                        color: cs.onSurface.withValues(alpha: .45))),
              ],
            ),
          ),
        ]),
      ),
    );
  }

  Widget _tag(String text, Color color) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
        decoration: BoxDecoration(
            color: color.withValues(alpha: .16),
            borderRadius: BorderRadius.circular(4)),
        child: Text(text,
            style: TextStyle(
                fontSize: 9.5, color: color, fontWeight: FontWeight.w600)),
      );

  Widget _groupHeader(String label, List<Map> items) {
    final key = '$_groupBy:$label';
    final collapsed = _collapsed.contains(key);
    final online = items.where((d) => d['Status'] == 'online').length;
    final rssis =
        items.map((d) => d['rssi']).where((v) => v != null).cast<int>().toList();
    final avg = rssis.isEmpty
        ? null
        : (rssis.reduce((a, b) => a + b) / rssis.length).round();
    final cs = Theme.of(context).colorScheme;
    return InkWell(
      onTap: () => setState(() =>
          collapsed ? _collapsed.remove(key) : _collapsed.add(key)),
      child: Container(
        color: Brand.surfaceAlt,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
        child: Row(children: [
          Icon(collapsed ? Icons.chevron_right_rounded : Icons.expand_more_rounded,
              size: 17, color: Brand.teal),
          const SizedBox(width: 5),
          Text(label,
              style: const TextStyle(
                  fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: .2)),
          const SizedBox(width: 9),
          Expanded(
            child: Text(
              '${items.length} device${items.length == 1 ? '' : 's'} · $online online'
              '${avg != null ? ' · avg $avg dBm' : ''}',
              style: TextStyle(
                  fontSize: 11, color: cs.onSurface.withValues(alpha: .45)),
            ),
          ),
        ]),
      ),
    );
  }

  Widget _controlBar() {
    Widget chip(String label, bool on, VoidCallback tap) => FilterChip(
          label: Text(label),
          selected: on,
          showCheckmark: false,
          onSelected: (_) => setState(tap),
          visualDensity: VisualDensity.compact,
          materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
        );
    final search = TextField(
      style: const TextStyle(fontSize: 13),
      decoration: const InputDecoration(
        prefixIcon: Icon(Icons.search_rounded, size: 17),
        prefixIconConstraints: BoxConstraints(minWidth: 34, minHeight: 30),
        hintText: 'Search all fields…',
      ),
      onChanged: (v) => setState(() => _filter = v),
    );
    final group = _dropdown('Group', _groupBy, const {
      '': 'none',
      'band': 'Band',
      'category': 'Category',
      'Status': 'Status',
      'signal': 'Signal',
    }, (v) => setState(() {
          _groupBy = v;
          _collapsed.clear();
        }));
    final sort = _dropdown('Sort', _sortKey, const {
      'IP': 'IP',
      'Name': 'Name',
      'signal': 'Signal',
      'Status': 'Status',
    }, (v) => setState(() => _sortKey = v));
    final sortDir = IconButton(
      tooltip: _sortAsc ? 'Ascending' : 'Descending',
      visualDensity: VisualDensity.compact,
      icon: Icon(
          _sortAsc ? Icons.arrow_upward_rounded : Icons.arrow_downward_rounded,
          size: 16),
      onPressed: () => setState(() => _sortAsc = !_sortAsc),
    );
    final chips = [
      chip('Weak', _onlyWeak, () => _onlyWeak = !_onlyWeak),
      chip('Offline', _onlyOffline, () => _onlyOffline = !_onlyOffline),
      chip('Unnamed', _onlyUnnamed, () => _onlyUnnamed = !_onlyUnnamed),
      chip('Hide defunct', _hideDefunct, () => _hideDefunct = !_hideDefunct),
    ];

    // One row in a Mac window; search over a wrapped control row on a phone.
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
      child: LayoutBuilder(builder: (ctx, box) {
        if (box.maxWidth >= 900) {
          return Row(children: [
            SizedBox(width: 230, child: search),
            const SizedBox(width: 10),
            group,
            const SizedBox(width: 8),
            sort,
            sortDir,
            const Spacer(),
            Flexible(
              child: Wrap(
                  spacing: 6,
                  alignment: WrapAlignment.end,
                  children: chips),
            ),
          ]);
        }
        return Column(children: [
          search,
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [group, sort, sortDir, ...chips],
          ),
        ]);
      }),
    );
  }

  Widget _dropdown(String label, String value, Map<String, String> opts,
          ValueChanged<String> onChanged) =>
      Row(mainAxisSize: MainAxisSize.min, children: [
        Text('$label ',
            style: TextStyle(fontSize: 11.5, color: Brand.offline)),
        DropdownButton<String>(
          value: value,
          isDense: true,
          underline: const SizedBox.shrink(),
          borderRadius: BorderRadius.circular(9),
          style: TextStyle(
              fontSize: 12.5, color: Theme.of(context).colorScheme.onSurface),
          items: opts.entries
              .map((e) =>
                  DropdownMenuItem(value: e.key, child: Text(e.value)))
              .toList(),
          onChanged: (v) => onChanged(v ?? value),
        ),
      ]);

  // ── alerts tab ────────────────────────────────────────────────────────────

  Widget _alertList() {
    if (_alerts.isEmpty) {
      return Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(Icons.check_circle_rounded, size: 32, color: Brand.good),
          const SizedBox(height: 10),
          const Text('No alerts', style: TextStyle(fontSize: 14)),
          const SizedBox(height: 4),
          Text('Every device is behaving.',
              style: TextStyle(fontSize: 12, color: Brand.offline)),
        ]),
      );
    }
    IconData ic(String s) => s == 'high'
        ? Icons.error_rounded
        : (s == 'med' ? Icons.warning_rounded : Icons.info_rounded);
    Color cl(String s) =>
        s == 'high' ? Brand.weak : (s == 'med' ? Brand.fair : Brand.offline);
    final cs = Theme.of(context).colorScheme;
    return ListView.separated(
      padding: EdgeInsets.zero,
      itemCount: _alerts.length,
      separatorBuilder: (context, i) => const Divider(),
      itemBuilder: (c, i) {
        final a = _alerts[i] as Map<String, dynamic>;
        final sev = '${a['severity']}';
        final rssi = a['rssi'] != null ? '  ·  ${a['rssi']} dBm' : '';
        return Container(
          height: Brand.rowH,
          padding: const EdgeInsets.symmetric(horizontal: 14),
          decoration: BoxDecoration(
            border: Border(left: BorderSide(color: cl(sev), width: 2.5)),
          ),
          child: Row(children: [
            Container(
              width: 30,
              height: 30,
              decoration: BoxDecoration(
                  color: cl(sev).withValues(alpha: .14),
                  borderRadius: BorderRadius.circular(8)),
              child: Icon(ic(sev), size: 17, color: cl(sev)),
            ),
            const SizedBox(width: 11),
            Expanded(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('${a['desc']}',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontSize: 13.5, height: 1.2, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 2),
                  Text('${a['device']}  ·  ${a['ip']}  ·  ${a['mac']}$rssi',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                          fontSize: 11.5,
                          height: 1.2,
                          color: cs.onSurface.withValues(alpha: .45))),
                ],
              ),
            ),
            const SizedBox(width: 10),
            _tag('${a['rule']}', cl(sev)),
          ]),
        );
      },
    );
  }
}
