import 'package:flutter/material.dart';

import '../design_system.dart';
import '../models.dart';

class StatusDot extends StatelessWidget {
  const StatusDot(this.status, {super.key, this.showLabel = true});
  final HealthStatus status;
  final bool showLabel;

  Color get color => switch (status) {
    HealthStatus.healthy => const Color(0xFF138A4B),
    HealthStatus.degraded => const Color(0xFFD59000),
    HealthStatus.down => const Color(0xFFC5362F),
    HealthStatus.maintenance => const Color(0xFF356AA0),
    HealthStatus.unknown => const Color(0xFF7C8793),
  };

  String get label => switch (status) {
    HealthStatus.healthy => 'سليم',
    HealthStatus.degraded => 'متدهور',
    HealthStatus.down => 'متوقف',
    HealthStatus.maintenance => 'صيانة',
    HealthStatus.unknown => 'غير معروف',
  };

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(
        width: 10,
        height: 10,
        decoration: BoxDecoration(color: color, shape: BoxShape.circle),
      ),
      if (showLabel) ...[
        const SizedBox(width: 7),
        Text(
          label,
          style: TextStyle(color: color, fontWeight: FontWeight.w700),
        ),
      ],
    ],
  );
}

class UsageBar extends StatelessWidget {
  const UsageBar({
    super.key,
    required this.label,
    required this.value,
    required this.icon,
  });
  final String label;
  final double? value;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final safe = (value ?? 0).clamp(0, 100).toDouble();
    final color = safe >= 85
        ? const Color(0xFFC5362F)
        : safe >= 70
        ? const Color(0xFFD59000)
        : const Color(0xFF138A4B);
    final ops = context.ops;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(icon, size: 18, color: ops.slate),
            const SizedBox(width: 7),
            Expanded(child: Text(label, style: TextStyle(color: ops.ink))),
            Text(
              value == null ? '-' : '${safe.toStringAsFixed(1)}%',
              style: TextStyle(fontWeight: FontWeight.w800, color: ops.ink),
            ),
          ],
        ),
        const SizedBox(height: 8),
        ClipRRect(
          borderRadius: BorderRadius.circular(3),
          child: LinearProgressIndicator(
            value: safe / 100,
            minHeight: 7,
            backgroundColor: ops.line,
            color: color,
          ),
        ),
      ],
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
  });
  final IconData icon;
  final String title;
  final String message;
  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 34, horizontal: 16),
      child: Column(
        children: [
          Icon(icon, size: 42, color: ops.muted),
          const SizedBox(height: 12),
          Text(
            title,
            style: Theme.of(context).textTheme.titleMedium?.copyWith(
              fontWeight: FontWeight.w800,
              color: ops.ink,
            ),
          ),
          const SizedBox(height: 5),
          Text(
            message,
            textAlign: TextAlign.center,
            style: TextStyle(color: ops.slate),
          ),
        ],
      ),
    );
  }
}
