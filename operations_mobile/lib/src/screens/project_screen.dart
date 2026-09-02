import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../models.dart';
import '../state.dart';
import '../widgets/status_widgets.dart';

class ProjectScreen extends ConsumerWidget {
  const ProjectScreen({
    super.key,
    required this.projectId,
    required this.projectName,
  });
  final int projectId;
  final String projectName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final details = ref.watch(projectProvider(projectId));
    return Scaffold(
      appBar: AppBar(
        title: Text(
          projectName,
          style: const TextStyle(fontWeight: FontWeight.w900),
        ),
        bottom: const PreferredSize(
          preferredSize: Size.fromHeight(1),
          child: Divider(height: 1),
        ),
        actions: [
          IconButton(
            tooltip: 'تحديث',
            onPressed: () => ref.invalidate(projectProvider(projectId)),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: details.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(
              error is ApiException ? error.message : 'تعذر تحميل المشروع.',
            ),
          ),
        ),
        data: (data) => RefreshIndicator(
          onRefresh: () async => ref.refresh(projectProvider(projectId).future),
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 1100),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _ProjectHeader(
                      project: data.project,
                      onCheck: () => _runCheck(context, ref, data.project),
                    ),
                    const SizedBox(height: 14),
                    _MetricChart(metrics: data.metrics),
                    const SizedBox(height: 14),
                    _ServicesPanel(
                      project: data.project,
                      onRestart: (service) => _confirmAction(
                        context,
                        ref,
                        data.project,
                        'restart_service',
                        service: service,
                      ),
                    ),
                    const SizedBox(height: 14),
                    _ChecksPanel(checks: data.checks),
                    const SizedBox(height: 14),
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(16),
                        child: Row(
                          children: [
                            const Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    'نسخة احتياطية فورية',
                                    style: TextStyle(
                                      fontWeight: FontWeight.w900,
                                    ),
                                  ),
                                  SizedBox(height: 4),
                                  Text(
                                    'ينشئ نسخة مدققة من بيانات المشروع عبر وكيل العمليات.',
                                    style: TextStyle(color: Color(0xFF677381)),
                                  ),
                                ],
                              ),
                            ),
                            OutlinedButton.icon(
                              onPressed: () => _confirmAction(
                                context,
                                ref,
                                data.project,
                                'create_backup',
                              ),
                              icon: const Icon(Icons.backup_outlined),
                              label: const Text('إنشاء نسخة'),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _runCheck(
    BuildContext context,
    WidgetRef ref,
    ProjectInfo project,
  ) async {
    try {
      await ref.read(apiProvider).runAction(project.id, 'check_now');
      ref.invalidate(projectProvider(project.id));
      ref.invalidate(dashboardProvider);
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('اكتمل فحص المشروع.')));
      }
    } on ApiException catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  Future<void> _confirmAction(
    BuildContext context,
    WidgetRef ref,
    ProjectInfo project,
    String action, {
    ServiceInfo? service,
  }) async {
    final controller = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(
          action == 'create_backup'
              ? 'إنشاء نسخة احتياطية'
              : 'إعادة تشغيل ${service?.name ?? ''}',
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'سيُسجل هذا الإجراء باسم حسابك. اكتب معرف المشروع للتأكيد:',
            ),
            const SizedBox(height: 10),
            SelectableText(
              project.slug,
              style: const TextStyle(fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 10),
            TextField(
              controller: controller,
              textDirection: TextDirection.ltr,
              decoration: const InputDecoration(labelText: 'معرف المشروع'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('إلغاء'),
          ),
          ElevatedButton(
            onPressed: () =>
                Navigator.pop(dialogContext, controller.text == project.slug),
            child: const Text('تأكيد التنفيذ'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (confirmed != true || !context.mounted) return;
    try {
      await ref
          .read(apiProvider)
          .runAction(
            project.id,
            action,
            serviceId: service?.id,
            confirmation: project.slug,
          );
      ref.invalidate(projectProvider(project.id));
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('تم إرسال الإجراء إلى مركز العمليات.')),
        );
      }
    } on ApiException catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }
}

class _ProjectHeader extends StatelessWidget {
  const _ProjectHeader({required this.project, required this.onCheck});
  final ProjectInfo project;
  final VoidCallback onCheck;
  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Wrap(
        runSpacing: 14,
        spacing: 16,
        alignment: WrapAlignment.spaceBetween,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          SizedBox(
            width: 520,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    StatusDot(project.status),
                    const SizedBox(width: 12),
                    if (project.latencyMs != null)
                      Text(
                        '${project.latencyMs} ms',
                        textDirection: TextDirection.ltr,
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  project.baseUrl.isEmpty
                      ? 'مشروع مكتشف من Docker دون رابط فحص عام'
                      : project.baseUrl,
                  textDirection: TextDirection.ltr,
                  textAlign: TextAlign.right,
                  style: const TextStyle(color: Color(0xFF596674)),
                ),
                if (project.lastCheckedAt != null)
                  Text(
                    'آخر فحص ${DateFormat('yyyy/MM/dd HH:mm').format(project.lastCheckedAt!)}',
                    style: const TextStyle(
                      color: Color(0xFF7C8793),
                      fontSize: 12,
                    ),
                  ),
              ],
            ),
          ),
          ElevatedButton.icon(
            onPressed: onCheck,
            icon: const Icon(Icons.monitor_heart_outlined),
            label: const Text('فحص الآن'),
          ),
        ],
      ),
    ),
  );
}

class _MetricChart extends StatelessWidget {
  const _MetricChart({required this.metrics});
  final List<MetricPoint> metrics;
  @override
  Widget build(BuildContext context) {
    final ordered = metrics.reversed.toList();
    List<FlSpot> spots(double? Function(MetricPoint) read) => [
      for (var i = 0; i < ordered.length; i++)
        if (read(ordered[i]) != null) FlSpot(i.toDouble(), read(ordered[i])!),
    ];
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'استهلاك المشروع المستقل',
              style: TextStyle(fontSize: 17, fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 5),
            const Text(
              'CPU وRAM نسبةً إلى إجمالي قدرة الخادم، مع حركة الشبكة وكتابة القرص الخاصة بحاويات المشروع.',
              style: TextStyle(color: Color(0xFF677381)),
            ),
            const SizedBox(height: 18),
            if (ordered.isEmpty)
              const EmptyState(
                icon: Icons.show_chart,
                title: 'لا توجد قياسات بعد',
                message: 'ستظهر بعد تشغيل دورة المراقبة الأولى.',
              )
            else ...[
              _CurrentUsage(metric: metrics.first),
              const SizedBox(height: 18),
              SizedBox(
                height: 210,
                child: LineChart(
                  LineChartData(
                    minY: 0,
                    maxY: 100,
                    gridData: const FlGridData(
                      show: true,
                      drawVerticalLine: false,
                    ),
                    borderData: FlBorderData(show: false),
                    titlesData: const FlTitlesData(
                      topTitles: AxisTitles(
                        sideTitles: SideTitles(showTitles: false),
                      ),
                      rightTitles: AxisTitles(
                        sideTitles: SideTitles(showTitles: false),
                      ),
                      bottomTitles: AxisTitles(
                        sideTitles: SideTitles(showTitles: false),
                      ),
                      leftTitles: AxisTitles(
                        sideTitles: SideTitles(
                          showTitles: true,
                          reservedSize: 34,
                          interval: 25,
                        ),
                      ),
                    ),
                    lineBarsData: [
                      LineChartBarData(
                        spots: spots((m) => m.cpu),
                        color: const Color(0xFF356AA0),
                        barWidth: 3,
                        dotData: const FlDotData(show: false),
                      ),
                      LineChartBarData(
                        spots: spots((m) => m.memory),
                        color: const Color(0xFF138A4B),
                        barWidth: 3,
                        dotData: const FlDotData(show: false),
                      ),
                    ],
                  ),
                ),
              ),
            ],
            const SizedBox(height: 12),
            const Wrap(
              spacing: 18,
              children: [
                _Legend(color: Color(0xFF356AA0), label: 'المعالج'),
                _Legend(color: Color(0xFF138A4B), label: 'الذاكرة'),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _CurrentUsage extends StatelessWidget {
  const _CurrentUsage({required this.metric});
  final MetricPoint metric;

  @override
  Widget build(BuildContext context) => Wrap(
    spacing: 10,
    runSpacing: 10,
    children: [
      _UsageChip(label: 'CPU', value: _percent(metric.cpu)),
      _UsageChip(label: 'RAM', value: _memory(metric)),
      _UsageChip(
        label: 'الحاويات',
        value: '${metric.runningContainerCount}/${metric.containerCount}',
      ),
      _UsageChip(
        label: 'الشبكة',
        value: '↓ ${_mb(metric.networkRxMb)}  ↑ ${_mb(metric.networkTxMb)}',
      ),
      _UsageChip(
        label: 'القرص I/O',
        value:
            'قراءة ${_mb(metric.blockReadMb)} · كتابة ${_mb(metric.blockWriteMb)}',
      ),
    ],
  );

  static String _percent(double? value) =>
      value == null ? 'غير متاح' : '${value.toStringAsFixed(1)}%';

  static String _mb(double? value) =>
      value == null ? '—' : '${value.toStringAsFixed(1)} MB';

  static String _memory(MetricPoint metric) {
    final used = _mb(metric.memoryUsedMb);
    final percent = _percent(metric.memory);
    return '$used · $percent';
  }
}

class _UsageChip extends StatelessWidget {
  const _UsageChip({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
    decoration: BoxDecoration(
      color: const Color(0xFFF2F7F4),
      border: Border.all(color: const Color(0xFFD8E4DC)),
      borderRadius: BorderRadius.circular(12),
    ),
    child: Text(
      '$label: $value',
      textDirection: TextDirection.rtl,
      style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w800),
    ),
  );
}

class _Legend extends StatelessWidget {
  const _Legend({required this.color, required this.label});
  final Color color;
  final String label;
  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(width: 12, height: 4, color: color),
      const SizedBox(width: 6),
      Text(label),
    ],
  );
}

class _ServicesPanel extends StatelessWidget {
  const _ServicesPanel({required this.project, required this.onRestart});
  final ProjectInfo project;
  final ValueChanged<ServiceInfo> onRestart;
  @override
  Widget build(BuildContext context) => Card(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Padding(
          padding: EdgeInsets.all(16),
          child: Text(
            'الخدمات',
            style: TextStyle(fontSize: 17, fontWeight: FontWeight.w900),
          ),
        ),
        const Divider(height: 1),
        if (project.services.isEmpty)
          const EmptyState(
            icon: Icons.widgets_outlined,
            title: 'لا توجد خدمات مسجلة',
            message: 'أضف خدمات المشروع من لوحة الإدارة.',
          )
        else
          ...project.services.map(
            (service) => ListTile(
              leading: StatusDot(service.status, showLabel: false),
              title: Text(
                service.name,
                style: const TextStyle(fontWeight: FontWeight.w800),
              ),
              subtitle: Text(service.kindLabel),
              trailing: service.restartAllowed
                  ? IconButton(
                      tooltip: 'إعادة تشغيل الخدمة',
                      onPressed: () => onRestart(service),
                      icon: const Icon(Icons.restart_alt),
                    )
                  : const Icon(
                      Icons.lock_outline,
                      size: 20,
                      color: Color(0xFF7C8793),
                    ),
            ),
          ),
      ],
    ),
  );
}

class _ChecksPanel extends StatelessWidget {
  const _ChecksPanel({required this.checks});
  final List<CheckPoint> checks;
  @override
  Widget build(BuildContext context) => Card(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Padding(
          padding: EdgeInsets.all(16),
          child: Text(
            'سجل فحوصات الصحة',
            style: TextStyle(fontSize: 17, fontWeight: FontWeight.w900),
          ),
        ),
        const Divider(height: 1),
        if (checks.isEmpty)
          const EmptyState(
            icon: Icons.fact_check_outlined,
            title: 'لا توجد فحوصات بعد',
            message: 'نفذ فحصًا الآن أو انتظر دورة المراقبة.',
          )
        else
          ...checks
              .take(12)
              .map(
                (check) => ListTile(
                  dense: true,
                  leading: Icon(
                    check.ok ? Icons.check_circle : Icons.cancel,
                    color: check.ok
                        ? const Color(0xFF138A4B)
                        : const Color(0xFFC5362F),
                  ),
                  title: Text(
                    check.ok ? 'استجابة سليمة' : 'فشل الفحص',
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  subtitle: Text(
                    check.checkedAt == null
                        ? '-'
                        : DateFormat(
                            'yyyy/MM/dd HH:mm:ss',
                          ).format(check.checkedAt!),
                  ),
                  trailing: Text(
                    check.latencyMs == null
                        ? (check.errorCode.isEmpty ? '-' : check.errorCode)
                        : '${check.latencyMs} ms',
                    textDirection: TextDirection.ltr,
                  ),
                ),
              ),
      ],
    ),
  );
}
