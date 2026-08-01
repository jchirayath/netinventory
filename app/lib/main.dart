// netinventory — macOS/iOS companion. Reads the read-only JSON API served by
// `netinv serve` (default http://127.0.0.1:8787). On iPhone, point Host at the
// Mac agent's LAN IP.
import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

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
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: const Color(0xFF1F4E78),
      ),
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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('netinventory'),
        actions: [
          IconButton(
              onPressed: _editHost,
              tooltip: 'Agent host',
              icon: const Icon(Icons.dns)),
          IconButton(
              onPressed: _loading ? null : _refresh,
              tooltip: 'Refresh',
              icon: const Icon(Icons.refresh)),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(30),
          child: _summaryBar(),
        ),
      ),
      body: _error != null
          ? _errorView()
          : _tab == 0
              ? _deviceList()
              : _alertList(),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: [
          const NavigationDestination(
              icon: Icon(Icons.devices), label: 'Devices'),
          NavigationDestination(
              icon: Badge(
                label: Text(
                    '${_alerts.where((a) => a['severity'] == 'high').length}'),
                isLabelVisible:
                    _alerts.any((a) => a['severity'] == 'high'),
                child: const Icon(Icons.notifications),
              ),
              label: 'Alerts'),
        ],
      ),
    );
  }

  Widget _summaryBar() {
    final s = _summary;
    final txt = s.isEmpty
        ? (_loading ? 'loading…' : '—')
        : '${s['total']} devices · ${s['online']} online · ${s['unnamed']} unnamed';
    return Padding(
      padding: const EdgeInsets.only(bottom: 6, left: 12, right: 12),
      child: Align(
          alignment: Alignment.centerLeft,
          child: Text(txt, style: const TextStyle(fontSize: 12))),
    );
  }

  Widget _errorView() => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.cloud_off, size: 40),
            const SizedBox(height: 12),
            Text('Cannot reach the agent\n$_error',
                textAlign: TextAlign.center),
            const SizedBox(height: 8),
            const Text(
                'Run `netinv serve` on your Mac, then set the host (top-right).',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.grey)),
            const SizedBox(height: 12),
            FilledButton(onPressed: _refresh, child: const Text('Retry')),
          ]),
        ),
      );

  Color _statusColor(String s) => s == 'online'
      ? Colors.green
      : (s == 'defunct' ? Colors.grey : Colors.orange);

  // --- Wi-Fi signal (RSSI) diagnostics -------------------------------------
  Color _signalColor(String sig) => sig == 'good'
      ? Colors.green
      : (sig == 'fair' ? Colors.orange : Colors.red);

  IconData _signalIcon(String sig) => sig == 'good'
      ? Icons.network_wifi
      : (sig == 'fair' ? Icons.network_wifi_2_bar : Icons.network_wifi_1_bar);

  String _signalReading(String sig) => sig == 'good'
      ? 'Good — solid link'
      : (sig == 'fair'
          ? 'Fair — usable but can drop under load'
          : 'Weak — likely cause of drops & failed DHCP');

  // Compact trailing indicator: signal (Wi-Fi devices) over the band label.
  Widget _signalTrailing(Map<String, dynamic> d) {
    final rssi = d['rssi'];
    final sig = '${d['signal']}';
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        if (rssi != null)
          Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(_signalIcon(sig), size: 15, color: _signalColor(sig)),
            const SizedBox(width: 3),
            Text('$rssi dBm',
                style: TextStyle(
                    fontSize: 11,
                    color: _signalColor(sig),
                    fontWeight:
                        sig == 'weak' ? FontWeight.bold : FontWeight.normal)),
          ]),
        Text('${d['band']}', style: const TextStyle(fontSize: 11)),
      ],
    );
  }

  void _showDevice(Map<String, dynamic> d) {
    final name = (d['Device'] as String?)?.isNotEmpty == true
        ? d['Device']
        : (d['Hostname'] as String?)?.isNotEmpty == true
            ? d['Hostname']
            : '(unnamed)';
    final rssi = d['rssi'];
    final sig = '${d['signal']}';
    final ctrl = TextEditingController(text: '${d['Device'] ?? ''}');
    showDialog(
      context: context,
      builder: (c) => AlertDialog(
        title: Text('$name'),
        content: Column(
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
                Icon(_signalIcon(sig), size: 18, color: _signalColor(sig)),
                const SizedBox(width: 6),
                Text('$rssi dBm',
                    style: TextStyle(
                        color: _signalColor(sig),
                        fontWeight: FontWeight.bold)),
              ]),
              const SizedBox(height: 4),
              Text(_signalReading(sig),
                  style: TextStyle(color: _signalColor(sig))),
            ] else
              const Text('No Wi-Fi signal reported (wired, or off the gateway).',
                  style: TextStyle(color: Colors.grey)),
            const Divider(height: 20),
            TextField(
              controller: ctrl,
              decoration: const InputDecoration(
                labelText: 'Device name',
                hintText: 'e.g. Living Room TV',
                isDense: true,
                border: OutlineInputBorder(),
              ),
              onSubmitted: (v) => _rename(c, d, v),
            ),
          ],
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
              width: 72,
              child: Text(k,
                  style: const TextStyle(color: Colors.grey, fontSize: 13))),
          Expanded(child: Text(v.isEmpty ? '—' : v)),
        ]),
      );

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
      Expanded(
        child: RefreshIndicator(
          onRefresh: _refresh,
          child: ListView.separated(
            itemCount: rows.length,
            separatorBuilder: (c, i) => const Divider(height: 1),
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
    final dim = d['Status'] != 'online';
    return ListTile(
      dense: true,
      onTap: () => _showDevice(d),
      leading: CircleAvatar(
          radius: 5, backgroundColor: _statusColor('${d['Status']}')),
      title: Text(deviceName(d),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(color: dim ? Colors.grey : null)),
      subtitle: Text('${d['IP']} · ${d['MAC']} · ${d['category']}',
          maxLines: 1, overflow: TextOverflow.ellipsis),
      trailing: _signalTrailing(d),
    );
  }

  Widget _groupHeader(String label, List<Map> items) {
    final key = '$_groupBy:$label';
    final collapsed = _collapsed.contains(key);
    final online = items.where((d) => d['Status'] == 'online').length;
    final rssis =
        items.map((d) => d['rssi']).where((v) => v != null).cast<int>().toList();
    final avg = rssis.isEmpty
        ? null
        : (rssis.reduce((a, b) => a + b) / rssis.length).round();
    return InkWell(
      onTap: () => setState(() =>
          collapsed ? _collapsed.remove(key) : _collapsed.add(key)),
      child: Container(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: Row(children: [
          Icon(collapsed ? Icons.chevron_right : Icons.expand_more, size: 18),
          const SizedBox(width: 4),
          Text(label, style: const TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              '${items.length} device${items.length == 1 ? '' : 's'} · $online online'
              '${avg != null ? ' · avg $avg dBm' : ''}',
              style: const TextStyle(fontSize: 12, color: Colors.grey),
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
          onSelected: (_) => setState(tap),
          visualDensity: VisualDensity.compact,
        );
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 8, 8, 0),
      child: Column(children: [
        TextField(
          decoration: const InputDecoration(
            prefixIcon: Icon(Icons.search),
            hintText: 'Search all fields…',
            isDense: true,
            border: OutlineInputBorder(),
          ),
          onChanged: (v) => setState(() => _filter = v),
        ),
        const SizedBox(height: 8),
        Wrap(spacing: 8, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
          _dropdown('Group', _groupBy, const {
            '': 'none',
            'band': 'Band',
            'category': 'Category',
            'Status': 'Status',
            'signal': 'Signal',
          }, (v) => setState(() {
                _groupBy = v;
                _collapsed.clear();
              })),
          _dropdown('Sort', _sortKey, const {
            'IP': 'IP',
            'Name': 'Name',
            'signal': 'Signal',
            'Status': 'Status',
          }, (v) => setState(() => _sortKey = v)),
          IconButton(
            tooltip: _sortAsc ? 'Ascending' : 'Descending',
            visualDensity: VisualDensity.compact,
            icon: Icon(_sortAsc ? Icons.arrow_upward : Icons.arrow_downward,
                size: 18),
            onPressed: () => setState(() => _sortAsc = !_sortAsc),
          ),
          chip('Only weak', _onlyWeak, () => _onlyWeak = !_onlyWeak),
          chip('Only offline', _onlyOffline, () => _onlyOffline = !_onlyOffline),
          chip('Only unnamed', _onlyUnnamed, () => _onlyUnnamed = !_onlyUnnamed),
          chip('Hide defunct', _hideDefunct, () => _hideDefunct = !_hideDefunct),
        ]),
        const SizedBox(height: 4),
      ]),
    );
  }

  Widget _dropdown(String label, String value, Map<String, String> opts,
          ValueChanged<String> onChanged) =>
      Row(mainAxisSize: MainAxisSize.min, children: [
        Text('$label ',
            style: const TextStyle(fontSize: 12, color: Colors.grey)),
        DropdownButton<String>(
          value: value,
          isDense: true,
          underline: const SizedBox.shrink(),
          style: Theme.of(context).textTheme.bodyMedium,
          items: opts.entries
              .map((e) =>
                  DropdownMenuItem(value: e.key, child: Text(e.value)))
              .toList(),
          onChanged: (v) => onChanged(v ?? value),
        ),
      ]);

  Widget _alertList() {
    if (_alerts.isEmpty) {
      return const Center(child: Text('No alerts'));
    }
    IconData ic(String s) =>
        s == 'high' ? Icons.error : (s == 'med' ? Icons.warning : Icons.info);
    Color cl(String s) => s == 'high'
        ? Colors.red
        : (s == 'med' ? Colors.orange : Colors.blueGrey);
    return ListView.separated(
      itemCount: _alerts.length,
      separatorBuilder: (context, i) => const Divider(height: 1),
      itemBuilder: (c, i) {
        final a = _alerts[i] as Map<String, dynamic>;
        final rssi = a['rssi'] != null ? ' · ${a['rssi']} dBm' : '';
        return ListTile(
          dense: true,
          leading: Icon(ic('${a['severity']}'), color: cl('${a['severity']}')),
          title: Text('${a['desc']}'),
          subtitle: Text('${a['device']} · ${a['ip']} · ${a['mac']}$rssi',
              maxLines: 1, overflow: TextOverflow.ellipsis),
          trailing:
              Text('${a['rule']}', style: const TextStyle(fontSize: 10)),
        );
      },
    );
  }
}
