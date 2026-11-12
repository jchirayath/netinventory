// Batteries tab: each battery device's level, last change and log, and a
// form to log a change. Data comes from /api/batteries (netinv battery.py).
import 'package:flutter/material.dart';

import 'api.dart';
import 'theme.dart';

// --- Pure view logic (unit-tested without a running app) -------------------

Color batteryColor(int? pct) => pct == null
    ? Brand.offline
    : pct <= 20
        ? Brand.weak
        : pct <= 50
            ? Brand.fair
            : Brand.good;

int? batteryPct(Map s) => s['pct'] is int ? s['pct'] as int : null;

/// Devices whose name, room, type, app, model or battery type contains [q]
/// (case-insensitive), keeping the agent's order (worst level first).
List<Map> filterBatteries(List devices, String q) {
  final needle = q.trim().toLowerCase();
  return devices
      .map((d) => d as Map)
      .where((d) =>
          needle.isEmpty ||
          [d['device'], d['name'], d['room'], d['type'], d['app_label'], d['model'],
                  d['battery']]
              .map((v) => '${v ?? ''}')
              .join(' ')
              .toLowerCase()
              .contains(needle))
      .toList();
}

/// The name to show: the friendly home.csv name, else the monitor's.
String batteryName(Map s) =>
    '${s['name'] ?? ''}'.isNotEmpty ? '${s['name']}' : '${s['device']}';

const batteryGroups = {'': 'None', 'room': 'Room', 'type': 'Type', 'app_label': 'App'};

/// Rows for the list: with [groupBy] ('room' / 'type' / 'app_label'), a
/// header map {'__grp': label, 'items': [...]} before each group's devices
/// (groups sorted by name, '(no room)' and the like last; a collapsed group
/// keeps only its header). Without, just [devices].
List groupBatteries(List<Map> devices, String groupBy, {Set<String> collapsed = const {}}) {
  if (groupBy.isEmpty) return devices;
  final none = '(no ${batteryGroups[groupBy]!.toLowerCase()})';
  final groups = <String, List<Map>>{};
  for (final d in devices) {
    final k = '${d[groupBy] ?? ''}';
    groups.putIfAbsent(k.isEmpty ? none : k, () => []).add(d);
  }
  final keys = groups.keys.toList()
    ..sort((a, b) => a == none
        ? 1
        : b == none
            ? -1
            : a.toLowerCase().compareTo(b.toLowerCase()));
  return [
    for (final k in keys) ...[
      {'__grp': k, 'items': groups[k]!},
      if (!collapsed.contains('$groupBy:$k')) ...groups[k]!,
    ]
  ];
}

/// "84%", "10% (low)", or "no reading".
String batteryLevelText(Map s) {
  final p = batteryPct(s);
  if (p == null) return 'no reading';
  final label = '${s['label'] ?? ''}';
  return label.isNotEmpty && label != '$p' ? '$p% ($label)' : '$p%';
}

/// True when the expected next change ([s]['next_due'], YYYY-MM-DD) is
/// today or past.
bool batteryDue(Map s, String today) {
  final due = '${s['next_due'] ?? ''}';
  return due.isNotEmpty && due.compareTo(today) <= 0;
}

int lowBatteryCount(List devices) => devices
    .where((d) => batteryPct(d as Map) != null && batteryPct(d)! <= 20)
    .length;

String isoDate(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

// --- Widget -----------------------------------------------------------------

class BatteriesTab extends StatefulWidget {
  final Api api;
  final Map<String, dynamic> data; // /api/batteries
  final Future<void> Function() onChanged;
  const BatteriesTab(
      {super.key,
      required this.api,
      required this.data,
      required this.onChanged});

  @override
  State<BatteriesTab> createState() => _BatteriesTabState();
}

class _BatteriesTabState extends State<BatteriesTab> {
  String _q = '';
  String _groupBy = '';
  final Set<String> _collapsed = {};

  List get _devices => (widget.data['devices'] as List?) ?? const [];
  List get _log => (widget.data['log'] as List?) ?? const [];
  List<String> get _events =>
      ((widget.data['events'] as List?) ?? const ['replaced', 'charged', 'checked', 'note'])
          .map((e) => '$e')
          .toList();

  void _say(String msg) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));

  /// Run a write; when the agent wants the passcode, ask once and retry.
  Future<WriteResult> _write(Future<WriteResult> Function() send) async {
    var r = await send();
    for (var i = 0; i < 2 && r.needsPasscode && mounted; i++) {
      final p = await _askPasscode(retry: i > 0);
      if (p == null) break;
      widget.api.passcode = p;
      r = await send();
    }
    return r;
  }

  Future<String?> _askPasscode({bool retry = false}) {
    final ctrl = TextEditingController(text: widget.api.passcode);
    return showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(retry ? 'Wrong passcode' : 'Passcode'),
        content: TextField(
          controller: ctrl,
          obscureText: true,
          autofocus: true,
          decoration: const InputDecoration(
              helperText: 'Set on the agent with: netinv set-passcode'),
          onSubmitted: (v) => Navigator.pop(c, v),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(c, ctrl.text), child: const Text('OK')),
        ],
      ),
    );
  }

  String _explain(WriteResult r) => r.status == 403
      ? 'Agent is read-only: restart it with netinv serve --allow-write'
      : r.status == 429
          ? 'Too many wrong passcodes; try again in a few minutes'
          : r.error;

  Future<void> _openForm({String device = '', Map? entry}) async {
    final s = _devices.cast<Map>().where((x) => x['device'] == device);
    final initialDevice = '${entry?['Device'] ?? device}';
    TextEditingController? devField; // the Autocomplete's own field
    final bat = TextEditingController(
        text: entry?['Battery'] ?? (s.isNotEmpty ? '${s.first['battery'] ?? ''}' : ''));
    final lvl = TextEditingController(text: entry?['Level'] ?? '');
    final notes = TextEditingController(text: entry?['Notes'] ?? '');
    var event = '${entry?['Event'] ?? 'replaced'}';
    var date = DateTime.tryParse('${entry?['Date'] ?? ''}') ?? DateTime.now();
    final names = {
      ..._devices.map((d) => '${(d as Map)['device']}'),
      ..._log.map((e) => '${(e as Map)['Device']}'),
    }.toList()
      ..sort();

    final saved = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, setD) => AlertDialog(
          title: Text(entry == null ? 'Log battery change' : 'Edit entry'),
          content: SizedBox(
            width: 360,
            child: SingleChildScrollView(
              child: Column(mainAxisSize: MainAxisSize.min, children: [
                Autocomplete<String>(
                  initialValue: TextEditingValue(text: initialDevice),
                  optionsBuilder: (v) => names.where(
                      (n) => n.toLowerCase().contains(v.text.toLowerCase())),
                  fieldViewBuilder: (c, ctrl, focus, onSubmit) {
                    devField = ctrl;
                    return TextField(
                        controller: ctrl,
                        focusNode: focus,
                        decoration: const InputDecoration(labelText: 'Device'));
                  },
                ),
                const SizedBox(height: 8),
                Row(children: [
                  Expanded(
                    child: DropdownButtonFormField<String>(
                      initialValue: _events.contains(event) ? event : _events.first,
                      decoration: const InputDecoration(labelText: 'What happened'),
                      items: [
                        for (final e in _events)
                          DropdownMenuItem(value: e, child: Text(e))
                      ],
                      onChanged: (v) => setD(() => event = v ?? event),
                    ),
                  ),
                  const SizedBox(width: 10),
                  TextButton.icon(
                    icon: const Icon(Icons.event_rounded, size: 18),
                    label: Text(isoDate(date)),
                    onPressed: () async {
                      final d = await showDatePicker(
                          context: c,
                          initialDate: date,
                          firstDate: DateTime(2000),
                          lastDate: DateTime.now());
                      if (d != null) setD(() => date = d);
                    },
                  ),
                ]),
                TextField(
                    controller: bat,
                    decoration: const InputDecoration(
                        labelText: 'Battery', hintText: 'e.g. 2x CR123A')),
                TextField(
                    controller: lvl,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                        labelText: 'Level % (optional)')),
                TextField(
                    controller: notes,
                    maxLines: 3,
                    minLines: 1,
                    decoration: const InputDecoration(labelText: 'Notes')),
              ]),
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
            FilledButton(
                onPressed: () => Navigator.pop(c, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (saved != true || !mounted) return;
    final body = {
      'device': (devField?.text ?? initialDevice).trim(),
      'event': event,
      'date': isoDate(date),
      'battery': bat.text.trim(),
      'level': lvl.text.trim(),
      'notes': notes.text.trim(),
    };
    final r = await _write(() => entry == null
        ? widget.api.logBattery(body)
        : widget.api.editBattery('${entry['Id']}', body));
    if (!mounted) return;
    if (r.ok) {
      _say(entry == null
          ? 'Logged $event for ${body['device']}'
          : 'Updated ${body['device']}');
      await widget.onChanged();
    } else {
      _say('Not saved: ${_explain(r)}');
    }
  }

  Future<void> _editDetails(Map s) async {
    final device = '${s['device']}';
    final name = TextEditingController(text: batteryName(s));
    final rooms = ((widget.data['rooms'] as List?) ?? const []).map((r) => '$r').toList();
    final types = ((widget.data['types'] as List?) ?? const []).map((t) => '$t').toList();
    var room = rooms.contains('${s['room']}') ? '${s['room']}' : '';
    var type = types.contains('${s['type']}') ? '${s['type']}' : '';
    var tracked = s['tracked'] == true;
    final saved = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, setD) => AlertDialog(
          title: const Text('Edit details'),
          content: SizedBox(
            width: 360,
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              if (batteryName(s) != device)
                Align(
                    alignment: Alignment.centerLeft,
                    child: Text(device, style: TextStyle(fontSize: 12, color: Brand.offline))),
              TextField(
                  controller: name,
                  decoration: const InputDecoration(
                      labelText: 'Name', hintText: 'e.g. Office Humidor Thermometer')),
              DropdownButtonFormField<String>(
                initialValue: room,
                decoration: const InputDecoration(labelText: 'Room'),
                items: [
                  const DropdownMenuItem(value: '', child: Text('(none)')),
                  for (final r in rooms) DropdownMenuItem(value: r, child: Text(r)),
                ],
                onChanged: (v) => setD(() => room = v ?? ''),
              ),
              DropdownButtonFormField<String>(
                initialValue: type,
                decoration: const InputDecoration(labelText: 'Type'),
                items: [
                  const DropdownMenuItem(value: '', child: Text('(none)')),
                  for (final t in types) DropdownMenuItem(value: t, child: Text(t)),
                ],
                onChanged: (v) => setD(() => type = v ?? ''),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Track its battery'),
                subtitle: const Text('Alert at 20%. Off for plugged-in devices.'),
                value: tracked,
                onChanged: (v) => setD(() => tracked = v),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (saved != true || !mounted) return;
    final r = await _write(() => widget.api.setBatteryDevice(device,
        name: name.text.trim(), room: room, type: type, tracked: tracked));
    if (!mounted) return;
    _say(r.ok ? 'Saved ${name.text.trim()}' : 'Not saved: ${_explain(r)}');
    if (r.ok) await widget.onChanged();
  }

  Future<void> _delete(Map e) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Delete entry?'),
        content: Text('The ${e['Event']} entry for ${e['Device']} on ${e['Date']}.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(c, true), child: const Text('Delete')),
        ],
      ),
    );
    if (yes != true || !mounted) return;
    final r = await _write(() => widget.api.deleteBattery('${e['Id']}'));
    if (!mounted) return;
    _say(r.ok ? 'Deleted' : 'Not deleted: ${_explain(r)}');
    if (r.ok) await widget.onChanged();
  }

  void _showDevice(Map s) {
    final name = '${s['device']}'; // the monitor's name: what log entries use
    final entries = _log.cast<Map>().where((e) => e['Device'] == name).toList();
    final cs = Theme.of(context).colorScheme;
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (c) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: .6,
        builder: (c, scroll) => ListView(
          controller: scroll,
          padding: const EdgeInsets.fromLTRB(18, 14, 18, 24),
          children: [
            Row(children: [
              Expanded(
                child: Text(batteryName(s),
                    style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700)),
              ),
              TextButton.icon(
                icon: const Icon(Icons.edit_rounded, size: 16),
                label: const Text('Edit details'),
                onPressed: () {
                  Navigator.pop(c);
                  _editDetails(s);
                },
              ),
            ]),
            if ('${s['model'] ?? ''}'.isNotEmpty)
              Text('${s['model']}',
                  style: TextStyle(fontSize: 12, color: cs.onSurface.withValues(alpha: .55))),
            const SizedBox(height: 10),
            _levelBar(s, width: 220),
            const SizedBox(height: 10),
            _kv('Room', '${s['room'] ?? ''}'),
            _kv('Type', '${s['type'] ?? ''}'),
            _kv('App', '${s['app_label'] ?? ''}'),
            if (batteryName(s) != name) _kv('Monitor', name),
            _kv('Battery', '${s['battery'] ?? ''}'),
            _kv('Last changed', '${s['last_changed'] ?? ''}'),
            _kv('Days since', s['days_since'] == null ? '' : '${s['days_since']}'),
            _kv('Lasts about',
                s['avg_days'] == null ? '' : '${s['avg_days']} days'),
            _kv('Next due', '${s['next_due'] ?? ''}'),
            _kv('Reported by', '${s['provider'] ?? ''}'),
            const SizedBox(height: 12),
            Row(children: [
              const Text('Change log',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
              const Spacer(),
              FilledButton.tonalIcon(
                icon: const Icon(Icons.add_rounded, size: 18),
                label: const Text('Log change'),
                onPressed: () {
                  Navigator.pop(c);
                  _openForm(device: name);
                },
              ),
            ]),
            const SizedBox(height: 6),
            if (entries.isEmpty)
              Text('Nothing logged yet.',
                  style: TextStyle(fontSize: 12.5, color: Brand.offline)),
            for (final e in entries)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text(
                    '${e['Date']}  ·  ${e['Event']}'
                    '${'${e['Battery']}'.isNotEmpty ? '  ·  ${e['Battery']}' : ''}'
                    '${'${e['Level']}'.isNotEmpty ? '  ·  ${e['Level']}%' : ''}',
                    style: const TextStyle(fontSize: 13)),
                subtitle: '${e['Notes']}'.isEmpty && e['Source'] != 'auto'
                    ? null
                    : Text(
                        '${e['Source'] == 'auto' ? '(detected) ' : ''}${e['Notes']}',
                        style: TextStyle(
                            fontSize: 12, color: cs.onSurface.withValues(alpha: .6))),
                trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                  IconButton(
                      tooltip: 'Edit',
                      icon: const Icon(Icons.edit_rounded, size: 17),
                      onPressed: () {
                        Navigator.pop(c);
                        _openForm(entry: e);
                      }),
                  IconButton(
                      tooltip: 'Delete',
                      icon: const Icon(Icons.delete_outline_rounded, size: 18),
                      onPressed: () {
                        Navigator.pop(c);
                        _delete(e);
                      }),
                ]),
              ),
          ],
        ),
      ),
    );
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(children: [
          SizedBox(
              width: 100,
              child: Text(k, style: TextStyle(color: Brand.offline, fontSize: 12.5))),
          Expanded(child: Text(v.isEmpty ? '—' : v, style: const TextStyle(fontSize: 13))),
        ]),
      );

  Widget _levelBar(Map s, {double width = 64}) {
    final p = batteryPct(s);
    final color = batteryColor(p);
    return Row(mainAxisSize: MainAxisSize.min, children: [
      ClipRRect(
        borderRadius: BorderRadius.circular(4),
        child: SizedBox(
          width: width,
          height: 8,
          child: LinearProgressIndicator(
            value: (p ?? 0) / 100,
            backgroundColor: Brand.line,
            valueColor: AlwaysStoppedAnimation(color),
          ),
        ),
      ),
      const SizedBox(width: 8),
      Text(batteryLevelText(s),
          style: TextStyle(
              fontSize: 12.5,
              color: p == null ? Brand.offline : color,
              fontWeight: p != null && p <= 20 ? FontWeight.w700 : FontWeight.w500)),
    ]);
  }

  Widget _groupHeader(String label, List<Map> items) {
    final cs = Theme.of(context).colorScheme;
    final id = '$_groupBy:$label';
    final low = lowBatteryCount(items);
    return InkWell(
      onTap: () => setState(() => _collapsed.contains(id) ? _collapsed.remove(id) : _collapsed.add(id)),
      child: Container(
        color: Brand.surfaceAlt,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        child: Row(children: [
          Icon(_collapsed.contains(id) ? Icons.chevron_right_rounded : Icons.expand_more_rounded,
              size: 18),
          const SizedBox(width: 4),
          Text(label, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700)),
          const SizedBox(width: 10),
          Text('${items.length} device${items.length == 1 ? '' : 's'}',
              style: TextStyle(fontSize: 12, color: cs.onSurface.withValues(alpha: .55))),
          if (low > 0) ...[
            const SizedBox(width: 8),
            Text('$low low',
                style: TextStyle(
                    fontSize: 12, color: Brand.weak, fontWeight: FontWeight.w600)),
          ],
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final rows = groupBatteries(filterBatteries(_devices, _q), _groupBy, collapsed: _collapsed);
    final today = isoDate(DateTime.now());
    final cs = Theme.of(context).colorScheme;
    return Column(children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(12, 6, 12, 6),
        child: Row(children: [
          Expanded(
            child: TextField(
              decoration: const InputDecoration(
                  isDense: true,
                  prefixIcon: Icon(Icons.search_rounded, size: 18),
                  hintText: 'Search name, room, app'),
              onChanged: (v) => setState(() => _q = v),
            ),
          ),
          const SizedBox(width: 6),
          PopupMenuButton<String>(
            tooltip: 'Group by',
            initialValue: _groupBy,
            onSelected: (v) => setState(() => _groupBy = v),
            itemBuilder: (c) => [
              for (final e in batteryGroups.entries)
                PopupMenuItem(value: e.key, child: Text('Group: ${e.value}')),
            ],
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                const Icon(Icons.segment_rounded, size: 18),
                const SizedBox(width: 4),
                Text(batteryGroups[_groupBy]!, style: const TextStyle(fontSize: 12.5)),
              ]),
            ),
          ),
          const SizedBox(width: 6),
          FilledButton.icon(
            icon: const Icon(Icons.battery_charging_full_rounded, size: 18),
            label: const Text('Log'),
            onPressed: () => _openForm(),
          ),
        ]),
      ),
      const Divider(),
      Expanded(
        child: rows.isEmpty
            ? Center(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text(
                      _devices.isEmpty
                          ? 'No battery devices yet.\nOn the agent: netinv monitor suggest --apply'
                          : 'No batteries match',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Brand.offline, fontSize: 13)),
                ),
              )
            : RefreshIndicator(
                onRefresh: widget.onChanged,
                child: ListView.separated(
                  padding: EdgeInsets.zero,
                  itemCount: rows.length,
                  separatorBuilder: (c, i) => const Divider(),
                  itemBuilder: (c, i) {
                    final row = rows[i] as Map;
                    if (row['__grp'] != null) {
                      return _groupHeader('${row['__grp']}', (row['items'] as List).cast<Map>());
                    }
                    final s = row;
                    final due = batteryDue(s, today);
                    final last = '${s['last_changed'] ?? ''}';
                    // Where and what first, so two 'Garage door' locks read differently.
                    final sub = [
                      if ('${s['room'] ?? ''}'.isNotEmpty && _groupBy != 'room') '${s['room']}',
                      if ('${s['app_label'] ?? ''}'.isNotEmpty && _groupBy != 'app_label')
                        '${s['app_label']}',
                      if ('${s['battery'] ?? ''}'.isNotEmpty) '${s['battery']}',
                      last.isEmpty ? 'no change logged' : 'changed $last',
                      if ('${s['next_due'] ?? ''}'.isNotEmpty)
                        'next ~${s['next_due']}',
                    ].join('  ·  ');
                    return InkWell(
                      onTap: () => _showDevice(s),
                      child: Container(
                        height: Brand.rowH + 4,
                        padding: const EdgeInsets.symmetric(horizontal: 14),
                        decoration: BoxDecoration(
                          border: Border(
                              left: BorderSide(
                                  color: batteryColor(batteryPct(s)), width: 2.5)),
                        ),
                        child: Row(children: [
                          Expanded(
                            child: Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(batteryName(s),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                        fontSize: 13.5, fontWeight: FontWeight.w600)),
                                const SizedBox(height: 2),
                                Text(sub,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: TextStyle(
                                        fontSize: 11.5,
                                        color: due
                                            ? Brand.fair
                                            : cs.onSurface.withValues(alpha: .45))),
                              ],
                            ),
                          ),
                          const SizedBox(width: 10),
                          _levelBar(s),
                        ]),
                      ),
                    );
                  },
                ),
              ),
      ),
    ]);
  }
}
