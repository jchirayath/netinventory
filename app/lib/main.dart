// netinventory — macOS/iOS companion. Reads the read-only JSON API served by
// `netinv serve` (default http://127.0.0.1:8787). On iPhone, point Host at the
// Mac agent's LAN IP.
import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

void main() => runApp(const NetInvApp());

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

  Widget _deviceList() {
    final q = _filter.toLowerCase();
    final rows = _devices.where((d) {
      if (q.isEmpty) return true;
      return jsonEncode(d).toLowerCase().contains(q);
    }).toList();
    return Column(children: [
      Padding(
        padding: const EdgeInsets.all(8),
        child: TextField(
          decoration: const InputDecoration(
            prefixIcon: Icon(Icons.search),
            hintText: 'Filter devices…',
            isDense: true,
            border: OutlineInputBorder(),
          ),
          onChanged: (v) => setState(() => _filter = v),
        ),
      ),
      Expanded(
        child: RefreshIndicator(
          onRefresh: _refresh,
          child: ListView.separated(
            itemCount: rows.length,
            separatorBuilder: (context, i) => const Divider(height: 1),
            itemBuilder: (c, i) {
              final d = rows[i] as Map<String, dynamic>;
              final name = (d['Device'] as String?)?.isNotEmpty == true
                  ? d['Device']
                  : (d['Hostname'] as String?)?.isNotEmpty == true
                      ? d['Hostname']
                      : '(unnamed)';
              return ListTile(
                dense: true,
                leading: CircleAvatar(
                  radius: 5,
                  backgroundColor: _statusColor('${d['Status']}'),
                ),
                title: Text('$name',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text(
                    '${d['IP']} · ${d['MAC']} · ${d['category']}',
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                trailing:
                    Text('${d['band']}', style: const TextStyle(fontSize: 11)),
              );
            },
          ),
        ),
      ),
    ]);
  }

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
        return ListTile(
          dense: true,
          leading: Icon(ic('${a['severity']}'), color: cl('${a['severity']}')),
          title: Text('${a['desc']}'),
          subtitle: Text('${a['device']} · ${a['ip']} · ${a['mac']}',
              maxLines: 1, overflow: TextOverflow.ellipsis),
          trailing:
              Text('${a['rule']}', style: const TextStyle(fontSize: 10)),
        );
      },
    );
  }
}
