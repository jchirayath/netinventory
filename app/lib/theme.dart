// netinventory — brand tokens and theme.
//
// One accent (teal) on a deep slate ground, matching the app mark. Everything
// is deliberately tight: this is a dense inventory tool, not a marketing page,
// so paddings are small and rows are short enough to see a whole subnet at once.
import 'package:flutter/material.dart';

class Brand {
  static const teal = Color(0xFF2DD4BF);
  static const tealSoft = Color(0xFF5EEAD4);
  static const ink = Color(0xFF0B1220); // app background
  static const surface = Color(0xFF111A2B); // cards / rows
  static const surfaceAlt = Color(0xFF16223A); // group headers, chips
  static const line = Color(0xFF223148);

  static const good = Color(0xFF34D399);
  static const fair = Color(0xFFFBBF24);
  static const weak = Color(0xFFF87171);
  static const offline = Color(0xFF64748B);

  /// Row height for the device list. Small on purpose.
  static const rowH = 52.0;
}

ThemeData buildTheme(Brightness b) {
  final dark = b == Brightness.dark;
  final scheme = ColorScheme.fromSeed(
    seedColor: Brand.teal,
    brightness: b,
  ).copyWith(
    primary: dark ? Brand.teal : const Color(0xFF0F766E),
    surface: dark ? Brand.surface : Colors.white,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: dark ? Brand.ink : const Color(0xFFF6F8FB),
    visualDensity: VisualDensity.compact,
    dividerTheme: DividerThemeData(
      space: 1,
      thickness: 1,
      color: dark ? Brand.line : const Color(0xFFE3E8EF),
    ),
    inputDecorationTheme: InputDecorationTheme(
      isDense: true,
      filled: true,
      fillColor: dark ? Brand.surfaceAlt : Colors.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(9),
        borderSide: BorderSide(color: dark ? Brand.line : const Color(0xFFD8DFE9)),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(9),
        borderSide: BorderSide(color: dark ? Brand.line : const Color(0xFFD8DFE9)),
      ),
    ),
    chipTheme: ChipThemeData(
      side: BorderSide(color: dark ? Brand.line : const Color(0xFFD8DFE9)),
      backgroundColor: dark ? Brand.surfaceAlt : Colors.white,
      selectedColor: scheme.primary.withValues(alpha: dark ? .20 : .14),
      labelStyle: TextStyle(fontSize: 12, color: scheme.onSurface),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 0),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(7)),
    ),
  );
}

/// Category → glyph. Keeps the list scannable without reading a word of text.
IconData categoryIcon(String category) {
  final c = category.toLowerCase();
  if (c.contains('camera')) return Icons.videocam_rounded;
  if (c.contains('thermostat')) return Icons.thermostat_rounded;
  if (c.contains('garage')) return Icons.garage_rounded;
  if (c.contains('lock')) return Icons.lock_rounded;
  if (c.contains('water') || c.contains('irrigation')) return Icons.water_drop_rounded;
  if (c.contains('energy') || c.contains('solar')) return Icons.solar_power_rounded;
  if (c.contains('printer')) return Icons.print_rounded;
  if (c.contains('voip')) return Icons.phone_in_talk_rounded;
  if (c.contains('tv') || c.contains('media')) return Icons.tv_rounded;
  if (c.contains('speaker') || c.contains('voice')) return Icons.speaker_rounded;
  if (c.contains('hub') || c.contains('bridge')) return Icons.hub_rounded;
  if (c.contains('plug') || c.contains('switch')) return Icons.power_rounded;
  if (c.contains('light')) return Icons.lightbulb_rounded;
  if (c.contains('phone') || c.contains('tablet')) return Icons.smartphone_rounded;
  if (c.contains('computer') || c.contains('nas')) return Icons.laptop_mac_rounded;
  if (c.contains('network')) return Icons.router_rounded;
  if (c.contains('vehicle')) return Icons.directions_car_rounded;
  if (c.contains('console')) return Icons.sports_esports_rounded;
  return Icons.devices_other_rounded;
}

/// Stable tint per category so the same kind of device always reads the same.
Color categoryTint(String category) {
  const palette = [
    Color(0xFF60A5FA), Color(0xFF34D399), Color(0xFFFBBF24), Color(0xFFF472B6),
    Color(0xFFA78BFA), Color(0xFF22D3EE), Color(0xFFFB923C), Color(0xFF4ADE80),
  ];
  if (category.isEmpty) return const Color(0xFF94A3B8);
  return palette[category.codeUnits.fold(0, (a, b) => a + b) % palette.length];
}
