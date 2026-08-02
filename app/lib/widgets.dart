// netinventory — small presentational widgets shared by the tabs.
import 'package:flutter/material.dart';
import 'theme.dart';

/// The app mark: a scanner hub with three discovered nodes. Drawn rather than
/// shipped as a raster so it stays crisp at every size the app renders it.
class BrandMark extends StatelessWidget {
  final double size;
  const BrandMark({super.key, this.size = 28});

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: size,
      height: size,
      child: CustomPaint(painter: _MarkPainter()),
    );
  }
}

class _MarkPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size s) {
    final u = s.width / 96.0;
    final stroke = Paint()
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..color = Brand.teal;
    final fill = Paint()..color = Brand.teal;

    // rounded tile
    final r = RRect.fromRectAndRadius(
        Rect.fromLTWH(0, 0, s.width, s.height), Radius.circular(22 * u));
    canvas.drawRRect(r, Paint()..color = Brand.surfaceAlt);

    // discovery sweep
    stroke.strokeWidth = 2 * u;
    stroke.color = Brand.teal.withValues(alpha: .35);
    for (final rad in [26.0, 36.0]) {
      canvas.drawArc(
          Rect.fromCircle(center: Offset(48 * u, 62 * u), radius: rad * u),
          3.34, 2.5, false, stroke);
    }

    // links hub → devices
    stroke.strokeWidth = 1.9 * u;
    stroke.color = Brand.teal.withValues(alpha: .6);
    for (final p in [Offset(26, 30), Offset(70, 30), Offset(48, 26)]) {
      canvas.drawLine(Offset(48 * u, 48 * u), Offset(p.dx * u, p.dy * u), stroke);
    }

    // devices
    stroke.strokeWidth = 2.3 * u;
    stroke.color = Brand.teal;
    for (final rect in [
      Rect.fromLTWH(17, 20, 18, 13),
      Rect.fromLTWH(61, 20, 18, 13),
    ]) {
      canvas.drawRRect(
          RRect.fromRectAndRadius(
              Rect.fromLTWH(rect.left * u, rect.top * u, rect.width * u,
                  rect.height * u),
              Radius.circular(2.5 * u)),
          stroke);
    }
    canvas.drawRRect(
        RRect.fromRectAndRadius(
            Rect.fromLTWH(42 * u, 12 * u, 12 * u, 17 * u), Radius.circular(3 * u)),
        stroke);

    // hub
    canvas.drawCircle(Offset(48 * u, 48 * u), 9.5 * u,
        Paint()..color = Brand.surfaceAlt);
    canvas.drawCircle(Offset(48 * u, 48 * u), 9.5 * u, stroke..strokeWidth = 2.6 * u);
    canvas.drawCircle(Offset(48 * u, 48 * u), 3.4 * u, fill);

    // inventory rows
    final bar = Paint()
      ..color = Brand.teal
      ..strokeCap = StrokeCap.round
      ..strokeWidth = 2.6 * u;
    canvas.drawLine(Offset(33 * u, 79 * u), Offset(63 * u, 79 * u), bar);
    bar.color = Brand.teal.withValues(alpha: .45);
    bar.strokeWidth = 2.2 * u;
    canvas.drawLine(Offset(40 * u, 86 * u), Offset(56 * u, 86 * u), bar);
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

/// Four-bar Wi-Fi strength meter. Reads at a glance where a dBm number doesn't.
class SignalBars extends StatelessWidget {
  final String signal; // good | fair | weak
  final int? rssi;
  const SignalBars({super.key, required this.signal, this.rssi});

  int get _filled => switch (signal) {
        'good' => 4,
        'fair' => 3,
        'weak' => 1,
        _ => 0,
      };

  Color get color => switch (signal) {
        'good' => Brand.good,
        'fair' => Brand.fair,
        'weak' => Brand.weak,
        _ => Brand.offline,
      };

  @override
  Widget build(BuildContext context) {
    if (rssi == null) {
      return Text('—',
          style: TextStyle(fontSize: 12, color: Brand.offline.withValues(alpha: .8)));
    }
    return Row(mainAxisSize: MainAxisSize.min, children: [
      for (var i = 0; i < 4; i++)
        Container(
          width: 3,
          height: 5.0 + i * 3,
          margin: const EdgeInsets.only(right: 2),
          decoration: BoxDecoration(
            color: i < _filled ? color : color.withValues(alpha: .18),
            borderRadius: BorderRadius.circular(1.5),
          ),
        ),
      const SizedBox(width: 5),
      Text('$rssi',
          style: TextStyle(
              fontSize: 12,
              height: 1,
              color: color,
              fontWeight: signal == 'weak' ? FontWeight.w700 : FontWeight.w500,
              fontFeatures: const [FontFeature.tabularFigures()])),
    ]);
  }
}

/// One number plus its label. Used in the header strip.
class StatPill extends StatelessWidget {
  final String value, label;
  final Color color;
  final IconData icon;
  const StatPill(
      {super.key,
      required this.value,
      required this.label,
      required this.color,
      required this.icon});

  @override
  Widget build(BuildContext context) {
    final onSurface = Theme.of(context).colorScheme.onSurface;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surface,
        borderRadius: BorderRadius.circular(9),
        border: Border.all(color: color.withValues(alpha: .28)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, size: 14, color: color),
        const SizedBox(width: 7),
        Text(value,
            style: TextStyle(
                fontSize: 15,
                height: 1,
                fontWeight: FontWeight.w700,
                color: onSurface,
                fontFeatures: const [FontFeature.tabularFigures()])),
        const SizedBox(width: 5),
        Text(label,
            style: TextStyle(
                fontSize: 11.5, height: 1, color: onSurface.withValues(alpha: .55))),
      ]),
    );
  }
}

/// Small square glyph tile for a device category.
class CategoryBadge extends StatelessWidget {
  final String category;
  final bool dim;
  const CategoryBadge({super.key, required this.category, this.dim = false});

  @override
  Widget build(BuildContext context) {
    final tint = dim ? Brand.offline : categoryTint(category);
    return Container(
      width: 30,
      height: 30,
      decoration: BoxDecoration(
        color: tint.withValues(alpha: .14),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Icon(categoryIcon(category), size: 17, color: tint),
    );
  }
}
