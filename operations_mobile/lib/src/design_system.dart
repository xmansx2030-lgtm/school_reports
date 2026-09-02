import 'package:flutter/material.dart';

/// Fixed brand colours that never change with the theme — used to seed the
/// light palette and on the always-dark gradient panels (where text stays
/// light in both themes).
abstract final class OpsColors {
  static const ink = Color(0xFF10231C);
  static const forest = Color(0xFF07583A);
  static const emerald = Color(0xFF0B8A5A);
  static const mint = Color(0xFFE7F4ED);
  static const gold = Color(0xFFC49A48);
  static const goldSoft = Color(0xFFF7EDD8);
  static const canvas = Color(0xFFF3F6F2);
  static const surface = Color(0xFFFCFDFC);
  static const line = Color(0xFFDCE6E0);
  static const slate = Color(0xFF66766F);
  static const danger = Color(0xFFB42318);
  static const warning = Color(0xFFB7791F);
  static const info = Color(0xFF236AA3);

  // On-dark tints for the gradient hero panels (identical in both themes).
  static const onDarkTitle = Colors.white;
  static const onDarkSubtitle = Color(0xFFC7D8D0);
  static const healthy = Color(0xFF58D68D);
}

/// Semantic, theme-aware palette. Read it with `context.ops` so every surface
/// and text colour flips correctly between light and dark.
@immutable
class OpsPalette extends ThemeExtension<OpsPalette> {
  const OpsPalette({
    required this.canvas,
    required this.surface,
    required this.surfaceAlt,
    required this.ink,
    required this.slate,
    required this.muted,
    required this.line,
    required this.lineSoft,
    required this.forest,
    required this.emerald,
    required this.mint,
    required this.gold,
    required this.goldSoft,
    required this.goldInk,
    required this.danger,
    required this.dangerSoft,
    required this.warning,
    required this.info,
    required this.healthy,
    required this.accentBlue,
  });

  final Color canvas; // scaffold background
  final Color surface; // cards / panels
  final Color surfaceAlt; // subtle inset surfaces (chips, tiles)
  final Color ink; // primary text
  final Color slate; // secondary text
  final Color muted; // tertiary text / captions
  final Color line; // default borders / dividers
  final Color lineSoft; // subtle chip borders
  final Color forest; // primary brand
  final Color emerald;
  final Color mint; // brand-tinted icon backgrounds
  final Color gold;
  final Color goldSoft; // "ahead / attention" backgrounds
  final Color goldInk; // text on goldSoft
  final Color danger;
  final Color dangerSoft; // error banner background
  final Color warning;
  final Color info;
  final Color healthy; // healthy status dot
  final Color accentBlue; // release chip icon

  static const light = OpsPalette(
    canvas: Color(0xFFF3F6F2),
    surface: Color(0xFFFCFDFC),
    surfaceAlt: Color(0xFFF4F7FA),
    ink: Color(0xFF10231C),
    slate: Color(0xFF5A6B63),
    muted: Color(0xFF7C8793),
    line: Color(0xFFDCE6E0),
    lineSoft: Color(0xFFD6DEE6),
    forest: Color(0xFF07583A),
    emerald: Color(0xFF0B8A5A),
    mint: Color(0xFFE7F4ED),
    gold: Color(0xFFC49A48),
    goldSoft: Color(0xFFF7EDD8),
    goldInk: Color(0xFF7A5A12),
    danger: Color(0xFFB42318),
    dangerSoft: Color(0xFFFDECEA),
    warning: Color(0xFFB7791F),
    info: Color(0xFF236AA3),
    healthy: Color(0xFF138A4B),
    accentBlue: Color(0xFF356AA0),
  );

  static const dark = OpsPalette(
    canvas: Color(0xFF0E1512),
    surface: Color(0xFF17211C),
    surfaceAlt: Color(0xFF1E2A24),
    ink: Color(0xFFE9F1EC),
    slate: Color(0xFFA6B7AF),
    muted: Color(0xFF85988F),
    line: Color(0xFF2A3831),
    lineSoft: Color(0xFF33443C),
    forest: Color(0xFF2FA877),
    emerald: Color(0xFF38C088),
    mint: Color(0xFF16352A),
    gold: Color(0xFFD8B061),
    goldSoft: Color(0xFF2E2614),
    goldInk: Color(0xFFE4C88A),
    danger: Color(0xFFE5675C),
    dangerSoft: Color(0xFF3A1E1B),
    warning: Color(0xFFE0A94A),
    info: Color(0xFF5C9BD1),
    healthy: Color(0xFF58D68D),
    accentBlue: Color(0xFF6FA8DA),
  );

  @override
  OpsPalette copyWith({
    Color? canvas,
    Color? surface,
    Color? surfaceAlt,
    Color? ink,
    Color? slate,
    Color? muted,
    Color? line,
    Color? lineSoft,
    Color? forest,
    Color? emerald,
    Color? mint,
    Color? gold,
    Color? goldSoft,
    Color? goldInk,
    Color? danger,
    Color? dangerSoft,
    Color? warning,
    Color? info,
    Color? healthy,
    Color? accentBlue,
  }) {
    return OpsPalette(
      canvas: canvas ?? this.canvas,
      surface: surface ?? this.surface,
      surfaceAlt: surfaceAlt ?? this.surfaceAlt,
      ink: ink ?? this.ink,
      slate: slate ?? this.slate,
      muted: muted ?? this.muted,
      line: line ?? this.line,
      lineSoft: lineSoft ?? this.lineSoft,
      forest: forest ?? this.forest,
      emerald: emerald ?? this.emerald,
      mint: mint ?? this.mint,
      gold: gold ?? this.gold,
      goldSoft: goldSoft ?? this.goldSoft,
      goldInk: goldInk ?? this.goldInk,
      danger: danger ?? this.danger,
      dangerSoft: dangerSoft ?? this.dangerSoft,
      warning: warning ?? this.warning,
      info: info ?? this.info,
      healthy: healthy ?? this.healthy,
      accentBlue: accentBlue ?? this.accentBlue,
    );
  }

  @override
  OpsPalette lerp(ThemeExtension<OpsPalette>? other, double t) {
    if (other is! OpsPalette) return this;
    return OpsPalette(
      canvas: Color.lerp(canvas, other.canvas, t)!,
      surface: Color.lerp(surface, other.surface, t)!,
      surfaceAlt: Color.lerp(surfaceAlt, other.surfaceAlt, t)!,
      ink: Color.lerp(ink, other.ink, t)!,
      slate: Color.lerp(slate, other.slate, t)!,
      muted: Color.lerp(muted, other.muted, t)!,
      line: Color.lerp(line, other.line, t)!,
      lineSoft: Color.lerp(lineSoft, other.lineSoft, t)!,
      forest: Color.lerp(forest, other.forest, t)!,
      emerald: Color.lerp(emerald, other.emerald, t)!,
      mint: Color.lerp(mint, other.mint, t)!,
      gold: Color.lerp(gold, other.gold, t)!,
      goldSoft: Color.lerp(goldSoft, other.goldSoft, t)!,
      goldInk: Color.lerp(goldInk, other.goldInk, t)!,
      danger: Color.lerp(danger, other.danger, t)!,
      dangerSoft: Color.lerp(dangerSoft, other.dangerSoft, t)!,
      warning: Color.lerp(warning, other.warning, t)!,
      info: Color.lerp(info, other.info, t)!,
      healthy: Color.lerp(healthy, other.healthy, t)!,
      accentBlue: Color.lerp(accentBlue, other.accentBlue, t)!,
    );
  }
}

extension OpsPaletteContext on BuildContext {
  OpsPalette get ops =>
      Theme.of(this).extension<OpsPalette>() ?? OpsPalette.light;
}

class PremiumPanel extends StatelessWidget {
  const PremiumPanel({
    required this.child,
    super.key,
    this.padding = const EdgeInsets.all(18),
    this.gradient,
    this.color,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final Gradient? gradient;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: gradient == null ? (color ?? ops.surface) : null,
        gradient: gradient,
        borderRadius: BorderRadius.circular(22),
        border: Border.all(
          color: gradient == null
              ? ops.line
              : Colors.white.withValues(alpha: .09),
        ),
        boxShadow: [
          BoxShadow(
            color: const Color(0x120B2A1E),
            blurRadius: 28,
            offset: const Offset(0, 12),
          ),
        ],
      ),
      child: child,
    );
  }
}

class SectionHeading extends StatelessWidget {
  const SectionHeading({
    required this.title,
    required this.icon,
    super.key,
    this.subtitle,
    this.trailing,
  });

  final String title;
  final String? subtitle;
  final IconData icon;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Row(
      children: [
        Container(
          width: 44,
          height: 44,
          decoration: BoxDecoration(
            color: ops.mint,
            borderRadius: BorderRadius.circular(14),
          ),
          child: Icon(icon, color: ops.forest),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w900,
                  color: ops.ink,
                ),
              ),
              if (subtitle != null)
                Text(
                  subtitle!,
                  style: TextStyle(color: ops.slate, fontSize: 12),
                ),
            ],
          ),
        ),
        ?trailing,
      ],
    );
  }
}
