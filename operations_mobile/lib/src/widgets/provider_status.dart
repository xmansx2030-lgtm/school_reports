import 'package:flutter/material.dart';

import '../models.dart';

String providerLocalDate(Object? value) {
  final date = value is DateTime
      ? value
      : DateTime.tryParse(value?.toString() ?? '');
  if (date == null) return 'غير متاح';
  final local = date.toLocal();
  String twoDigits(int number) => number.toString().padLeft(2, '0');
  return '${local.year.toString().padLeft(4, '0')}/'
      '${twoDigits(local.month)}/${twoDigits(local.day)} '
      '${twoDigits(local.hour)}:${twoDigits(local.minute)}';
}

List<Map<String, dynamic>> latestProviderBackups(
  List<Map<String, dynamic>> backups, {
  int limit = 7,
}) {
  final indexed = backups.indexed.toList();
  indexed.sort((a, b) {
    final first = DateTime.tryParse('${a.$2['created'] ?? ''}');
    final second = DateTime.tryParse('${b.$2['created'] ?? ''}');
    if (first == null && second == null) return a.$1.compareTo(b.$1);
    if (first == null) return 1;
    if (second == null) return -1;
    final order = second.compareTo(first);
    return order == 0 ? a.$1.compareTo(b.$1) : order;
  });
  return indexed.take(limit).map((item) => item.$2).toList();
}

class ProviderValueRow extends StatelessWidget {
  const ProviderValueRow({
    super.key,
    required this.label,
    required this.value,
    this.valueDirection = TextDirection.ltr,
  });

  final String label;
  final String value;
  final TextDirection valueDirection;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 3),
    child: LayoutBuilder(
      builder: (context, constraints) {
        final labelWidget = Text(label, textDirection: TextDirection.rtl);
        final valueWidget = Text(
          value,
          textDirection: valueDirection,
          style: const TextStyle(fontWeight: FontWeight.w600),
        );
        if (constraints.maxWidth < 250 ||
            MediaQuery.textScalerOf(context).scale(14) > 19) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [labelWidget, valueWidget],
          );
        }
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(child: labelWidget),
            const SizedBox(width: 12),
            Expanded(
              flex: 2,
              child: Align(
                alignment: AlignmentDirectional.centerEnd,
                child: valueWidget,
              ),
            ),
          ],
        );
      },
    ),
  );
}

class ProviderStatusSummary extends StatelessWidget {
  const ProviderStatusSummary({
    super.key,
    required this.provider,
    this.host,
    this.showName = false,
  });

  final ProviderServerInfo provider;
  final ServerInfo? host;
  final bool showName;

  String _percent(double? value) =>
      value == null ? '—' : '${value.toStringAsFixed(1)}%';

  DateTime? get _cpuCapturedAt {
    final series = provider.metrics['time_series'];
    if (series is! Map) return null;
    final cpu = series['cpu'];
    if (cpu is! Map) return null;
    final values = cpu['values'];
    if (values is! List || values.isEmpty) return null;
    final last = values.last;
    if (last is! List || last.isEmpty) return null;
    final seconds = num.tryParse('${last.first}');
    if (seconds == null || !seconds.isFinite) return null;
    return DateTime.fromMillisecondsSinceEpoch(
      (seconds * 1000).round(),
      isUtc: true,
    );
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      const Text('حالة Hetzner', style: TextStyle(fontWeight: FontWeight.bold)),
      const SizedBox(height: 6),
      if (showName) ProviderValueRow(label: 'الخادم', value: provider.name),
      ProviderValueRow(label: 'الحالة', value: provider.status),
      ProviderValueRow(label: 'المواصفات', value: provider.serverType),
      ProviderValueRow(label: 'الموقع', value: provider.location),
      ProviderValueRow(
        label: 'حماية الحذف',
        value: provider.deleteProtected ? 'مفعلة' : 'غير مفعلة',
        valueDirection: TextDirection.rtl,
      ),
      ProviderValueRow(
        label: 'النسخ التلقائية',
        value: provider.backupWindow.isEmpty ? 'غير مفعلة' : 'مفعلة',
        valueDirection: TextDirection.rtl,
      ),
      const Divider(height: 24),
      const Text('المصدر: Hetzner'),
      ProviderValueRow(
        label: 'المعالج',
        value: _percent(provider.latestMetric('cpu')),
      ),
      if (_cpuCapturedAt != null)
        ProviderValueRow(
          label: 'وقت القياس',
          value: providerLocalDate(_cpuCapturedAt),
        ),
      ProviderValueRow(
        label: 'آخر جلب',
        value: providerLocalDate(provider.fetchedAt),
      ),
      if (host != null) ...[
        const Divider(height: 24),
        const Text('المصدر: القياس داخل الخادم'),
        ProviderValueRow(label: 'الذاكرة', value: _percent(host!.memory)),
        ProviderValueRow(label: 'القرص', value: _percent(host!.disk)),
        ProviderValueRow(
          label: 'آخر جرد',
          value: providerLocalDate(host!.lastCheckedAt),
        ),
      ],
      const SizedBox(height: 4),
      Text(
        'الأوقات بتوقيت الجهاز',
        style: Theme.of(context).textTheme.bodySmall,
      ),
    ],
  );
}

class ProviderBackupList extends StatelessWidget {
  const ProviderBackupList({super.key, required this.backups});

  final List<Map<String, dynamic>> backups;

  Widget _backupTitle(Map<String, dynamic> backup) {
    final description = '${backup['description'] ?? ''}';
    final automatic = RegExp(
      r'^Backup \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$',
    ).hasMatch(description);
    if (automatic) {
      return Tooltip(message: description, child: const Text('نسخة احتياطية'));
    }
    return Text(description.trim().isEmpty ? 'نسخة احتياطية' : description);
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      const Text(
        'نسخ Hetzner الأخيرة',
        style: TextStyle(fontWeight: FontWeight.bold),
      ),
      Text(
        'الأحدث أولًا · بتوقيت الجهاز',
        style: Theme.of(context).textTheme.bodySmall,
      ),
      if (backups.isEmpty) const Text('لا توجد نسخ معروضة.'),
      for (final backup in latestProviderBackups(backups))
        ListTile(
          contentPadding: EdgeInsets.zero,
          dense: true,
          title: _backupTitle(backup),
          subtitle: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                providerLocalDate(backup['created']),
                textDirection: TextDirection.ltr,
              ),
              if (backup['id'] != null)
                ProviderValueRow(label: 'المعرّف', value: '${backup['id']}'),
              ProviderValueRow(
                label: 'الحالة',
                value: '${backup['status'] ?? ''}'.trim().isEmpty
                    ? 'غير متاحة'
                    : '${backup['status']}',
              ),
            ],
          ),
        ),
    ],
  );
}
