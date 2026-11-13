// Client for the JSON API served by `netinv serve`.
import 'dart:convert';
import 'package:http/http.dart' as http;

/// Result of a write: the HTTP status (0 = the agent was unreachable) and the
/// decoded body, so callers can show the agent's own error message.
class WriteResult {
  final int status;
  final Map<String, dynamic> body;
  const WriteResult(this.status, this.body);
  bool get ok => status == 200;

  /// The agent asked for the passcode (an edit from a device other than the
  /// Mac running `netinv serve --lan --allow-write`).
  bool get needsPasscode => status == 401;
  String get error => '${body['error'] ?? 'HTTP $status'}';
}

/// A read the agent answered with something other than 200. [status] lets a
/// caller tell "this agent doesn't have that endpoint" (404, an older
/// version) from a real failure that the user should see.
class ApiError implements Exception {
  final int status;
  const ApiError(this.status);
  bool get notFound => status == 404;
  @override
  String toString() => 'HTTP $status';
}

class Api {
  String base;

  /// Sent with every write; the agent only checks it for requests that don't
  /// come from its own machine. Set with `netinv set-passcode` on the agent.
  String passcode;
  Api(this.base, {this.passcode = ''});

  Future<dynamic> _get(String path) async {
    final r = await http
        .get(Uri.parse('$base$path'))
        .timeout(const Duration(seconds: 6));
    if (r.statusCode != 200) {
      throw ApiError(r.statusCode);
    }
    return jsonDecode(r.body);
  }

  Future<WriteResult> _post(String path, Map<String, dynamic> body) async {
    try {
      final r = await http
          .post(Uri.parse('$base$path'),
              headers: {
                'Content-Type': 'application/json',
                if (passcode.isNotEmpty) 'X-Netinv-Passcode': passcode,
              },
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 8));
      Map<String, dynamic> j = {};
      try {
        final d = jsonDecode(r.body);
        if (d is Map<String, dynamic>) j = d;
      } on FormatException {
        // not JSON (a proxy's HTML error page): the status alone tells the caller
      }
      return WriteResult(r.statusCode, j);
    } catch (_) {
      return const WriteResult(0, {'error': 'could not reach the agent'});
    }
  }

  Future<Map<String, dynamic>> summary() async =>
      (await _get('/api/summary')) as Map<String, dynamic>;
  Future<List<dynamic>> devices() async =>
      (await _get('/api/devices')) as List<dynamic>;
  Future<List<dynamic>> alerts() async =>
      (await _get('/api/alerts')) as List<dynamic>;

  /// {devices: [...summaries], log: [...entries], events: [...]}.
  Future<Map<String, dynamic>> batteries() async =>
      (await _get('/api/batteries')) as Map<String, dynamic>;

  /// {summary, log, history} for one device.
  Future<Map<String, dynamic>> battery(String device) async =>
      (await _get('/api/battery?device=${Uri.encodeQueryComponent(device)}'))
          as Map<String, dynamic>;

  /// Rename a device. Returns the HTTP status (200 ok, 401 passcode needed,
  /// 403 read-only, 404 unknown MAC), or 0 if the agent was unreachable.
  Future<int> setName(String mac, String name) async =>
      (await _post('/api/name', {'mac': mac, 'name': name})).status;

  /// Log a battery change: device, event, date, battery, level, notes.
  Future<WriteResult> logBattery(Map<String, dynamic> entry) =>
      _post('/api/battery-log', entry);
  Future<WriteResult> editBattery(String id, Map<String, dynamic> entry) =>
      _post('/api/battery-edit', {'id': id, ...entry});
  Future<WriteResult> deleteBattery(String id) =>
      _post('/api/battery-delete', {'id': id});

  /// Name, room and type of a battery device, and whether its battery is
  /// tracked (saved to the agent's home.csv / monitors.csv).
  Future<WriteResult> setBatteryDevice(String device,
          {required String name,
          required String room,
          required String type,
          required bool tracked}) =>
      _post('/api/battery-device',
          {'device': device, 'name': name, 'room': room, 'type': type, 'tracked': tracked});
}
