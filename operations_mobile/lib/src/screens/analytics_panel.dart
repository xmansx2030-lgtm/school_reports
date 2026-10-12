import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../models.dart';
import '../state.dart';

/// Explicit refresh keeps a reader's selected range and scroll position stable.
class AnalyticsPanel extends ConsumerStatefulWidget {
  const AnalyticsPanel({super.key, required this.id, this.server = false});
  final int id;
  final bool server;

  @override
  ConsumerState<AnalyticsPanel> createState() => _AnalyticsPanelState();
}

class _AnalyticsPanelState extends ConsumerState<AnalyticsPanel> {
  String _window = '48h';
  AnalyticsData? _data;
  String? _error;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final value = await ref
          .read(apiProvider)
          .analytics(widget.id, server: widget.server, window: _window);
      if (mounted) setState(() => _data = value);
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiException
              ? error.message
              : 'تعذر تحميل التحليلات.',
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final data = _data;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    widget.server
                        ? 'تحليلات الخادم والمشاريع'
                        : 'تحليلات المشروع',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                IconButton(
                  onPressed: _loading ? null : _load,
                  tooltip: 'تحديث التحليلات',
                  icon: const Icon(Icons.refresh),
                ),
              ],
            ),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final window in const {
                  '1h': 'ساعة',
                  '24h': '24 ساعة',
                  '48h': '48 ساعة',
                  '7d': '7 أيام',
                }.entries)
                  ChoiceChip(
                    label: Text(window.value),
                    selected: _window == window.key,
                    onSelected: _loading
                        ? null
                        : (_) {
                            setState(() => _window = window.key);
                            _load();
                          },
                  ),
              ],
            ),
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: LinearProgressIndicator(),
              ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Text(
                  '$_error${data != null ? ' تبقى آخر قراءة ناجحة معروضة أدناه.' : ''}',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            if (data != null) ...[
              const SizedBox(height: 12),
              Text(
                '${_time(data.start)} — ${_time(data.end)}',
                textDirection: TextDirection.ltr,
              ),
              Text(
                'المصدر: ${_source(data.source)} · التغطية ${_value(data.coverage['coverage_percent'], '%')} · ${data.coverage['sample_count'] ?? 0} عينة',
              ),
              Text(
                'آخر عينة: ${_time(DateTime.tryParse('${data.coverage['last_captured_at'] ?? ''}')?.toLocal())}',
              ),
              if (data.stale)
                Text(
                  'القياسات قديمة؛ تحقق من اتصال جامع الخادم.',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              if (data.coverage['history_truncated'] == true ||
                  data.summary['health_history_truncated'] == true)
                const Text(
                  'وصلت القراءة إلى حد السجل التاريخي؛ الملخص يصف البيانات المسترجعة فقط.',
                ),
              if (data.window != _window)
                const Text(
                  'تظهر بيانات الفترة السابقة إلى حين نجاح تحميل الفترة المختارة.',
                ),
              const SizedBox(height: 14),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  _metric(
                    'المعالج · متوسط / أعلى',
                    '${_value(data.summary['cpu_avg'], '%')} / ${_value(data.summary['cpu_max'], '%')}',
                  ),
                  _metric(
                    'الذاكرة · متوسط / أعلى',
                    '${_value(data.summary['memory_avg'], '%')} / ${_value(data.summary['memory_max'], '%')}',
                  ),
                  if (widget.server)
                    _metric(
                      'القرص · آخر قراءة',
                      _value(data.summary['disk_latest'], '%'),
                    ),
                  _metric(
                    'نجاح فحوص الوصول',
                    _value(data.summary['availability_percent'], '%'),
                  ),
                  _metric(
                    'الفحوص / الفاشلة',
                    '${data.summary['health_checks'] ?? 0} / ${data.summary['failed_checks'] ?? 0}',
                  ),
                  _metric(
                    'الاستجابة · متوسط / P95',
                    '${_value(data.summary['latency_avg_ms'], ' ms')} / ${_value(data.summary['latency_p95_ms'], ' ms')}',
                  ),
                  if (!widget.server) ...[
                    _metric(
                      'الذاكرة · متوسط / أعلى MB',
                      '${_value(data.summary['memory_used_avg_mb'], '')} / ${_value(data.summary['memory_used_max_mb'], '')}',
                    ),
                    _metric(
                      'الحاويات العاملة / الإجمالي',
                      '${data.latest['running_container_count'] ?? '—'} / ${data.latest['container_count'] ?? '—'}',
                    ),
                    _metric(
                      'الشبكة التراكمية · استقبال / إرسال MB',
                      '${_value(data.latest['network_rx_mb'], '')} / ${_value(data.latest['network_tx_mb'], '')}',
                    ),
                    _metric(
                      'القرص التراكمي · قراءة / كتابة MB',
                      '${_value(data.latest['block_read_mb'], '')} / ${_value(data.latest['block_write_mb'], '')}',
                    ),
                  ],
                ],
              ),
              const SizedBox(height: 16),
              if (data.series.isEmpty)
                const Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('لا توجد قياسات في هذه الفترة.'),
                )
              else
                _ResourceChart(data: data),
              const SizedBox(height: 10),
              const Text(
                'المعالج باللون الأزرق · الذاكرة باللون الأخضر. القيم نسب من قدرة الخادم؛ نسبة نجاح الفحوص لا تثبت استمرارية الخدمة بين الفحوص.',
              ),
              for (final note in data.notes)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    note,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              if (widget.server && data.projects.isNotEmpty) ...[
                const Divider(height: 28),
                Text(
                  'مقارنة المشاريع (${data.projects.length})',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                for (final project in data.projects) _projectRow(project),
              ],
              if (data.latest['host_details'] is Map)
                _hostDetails(
                  Map<String, dynamic>.from(data.latest['host_details'] as Map),
                ),
              if (data.latest['container_states'] is List)
                _containers(data.latest['container_states'] as List),
            ],
          ],
        ),
      ),
    );
  }

  Widget _metric(String label, String value) => Container(
    constraints: const BoxConstraints(minWidth: 145),
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      borderRadius: BorderRadius.circular(12),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: const TextStyle(fontSize: 12)),
        Text(
          value,
          textDirection: TextDirection.ltr,
          style: const TextStyle(fontWeight: FontWeight.bold),
        ),
      ],
    ),
  );

  Widget _projectRow(Map<String, dynamic> project) {
    final summary = project['summary'] is Map
        ? project['summary'] as Map
        : const {};
    final coverage = project['coverage'] is Map
        ? project['coverage'] as Map
        : const {};
    return ListTile(
      contentPadding: EdgeInsets.zero,
      title: Text('${project['name'] ?? project['slug'] ?? ''}'),
      subtitle: Text(
        'CPU ${_value(summary['cpu_avg'], '%')} · RAM ${_value(summary['memory_avg'], '%')} · نجاح الفحوص ${_value(summary['availability_percent'], '%')}\nالتغطية ${_value(coverage['coverage_percent'], '%')}${coverage['stale'] == true ? ' · قياسات قديمة' : ''}',
      ),
    );
  }

  Widget _hostDetails(Map<String, dynamic> details) => ExpansionTile(
    tilePadding: EdgeInsets.zero,
    title: const Text('تفاصيل موارد المضيف'),
    children: [
      for (final item in const {
        'cpu_count': 'الأنوية',
        'load1': 'الحمل خلال دقيقة',
        'load5': 'الحمل خلال 5 دقائق',
        'load15': 'الحمل خلال 15 دقيقة',
        'memory_available_mb': 'الذاكرة المتاحة MB',
        'memory_total_mb': 'إجمالي الذاكرة MB',
        'swap_used_mb': 'Swap المستخدم MB',
        'swap_total_mb': 'إجمالي Swap MB',
        'disk_free_gb': 'القرص المتاح GB',
        'disk_total_gb': 'إجمالي القرص GB',
        'disk_inode_percent': 'استهلاك inodes %',
        'iowait_percent': 'انتظار القرص %',
      }.entries)
        if (details.containsKey(item.key))
          ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            title: Text(item.value),
            trailing: Text(_value(details[item.key], '')),
          ),
      if (details['pressure'] is Map) ...[
        const Padding(
          padding: EdgeInsets.only(top: 8),
          child: Text('ضغط الموارد PSI · متوسط 10 / 60 / 300 ثانية'),
        ),
        for (final entry in (details['pressure'] as Map).entries)
          if (entry.value is Map)
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text('${entry.key}', textDirection: TextDirection.ltr),
              subtitle: Text(
                '${_value(entry.value['avg10'], '%')} / ${_value(entry.value['avg60'], '%')} / ${_value(entry.value['avg300'], '%')}',
                textDirection: TextDirection.ltr,
              ),
            ),
      ],
    ],
  );

  Widget _containers(List<dynamic> containers) => ExpansionTile(
    tilePadding: EdgeInsets.zero,
    initiallyExpanded: !widget.server,
    title: Text('حاويات المشروع (${containers.length})'),
    children: [
      for (final raw in containers)
        if (raw is Map)
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(
              '${raw['name'] ?? raw['service'] ?? raw['id'] ?? ''}',
              textDirection: TextDirection.ltr,
            ),
            subtitle: Text(
              'الحالة: ${raw['state'] ?? raw['status'] ?? 'غير معروفة'} · الصحة: ${raw['health'] ?? 'غير محددة'}\nCPU ${_value(raw['cpu_percent'], '%')} · RAM ${_value(raw['memory_used_mb'], ' MB')}${raw['restart_count'] != null ? ' · إعادات التشغيل ${raw['restart_count']}' : ''}\nالشبكة التراكمية MB: ↓ ${_value(raw['network_rx_mb'], '')} ↑ ${_value(raw['network_tx_mb'], '')}\nالقرص التراكمي MB: قراءة ${_value(raw['block_read_mb'], '')} · كتابة ${_value(raw['block_write_mb'], '')}${raw['oom_killed'] == true ? '\nتوقفت بسبب نفاد الذاكرة' : ''}',
            ),
          ),
    ],
  );
}

String _value(dynamic value, String unit) {
  final number = num.tryParse('$value');
  return number == null ? '—' : '${number.toStringAsFixed(1)}$unit';
}

String _time(DateTime? value) =>
    value == null ? 'غير متاح' : DateFormat('MM/dd HH:mm').format(value);
String _source(String value) => switch (value) {
  'host' => 'وكيل المضيف',
  'docker' => 'حاويات المشروع',
  'legacy_sampled' => 'قياسات تاريخية قديمة',
  'unavailable' => 'لا توجد قياسات',
  _ => value.isEmpty ? 'غير محدد' : value,
};

class _ResourceChart extends StatelessWidget {
  const _ResourceChart({required this.data});
  final AnalyticsData data;

  @override
  Widget build(BuildContext context) {
    List<FlSpot> points(String key) => [
      for (final row in data.series)
        if (DateTime.tryParse('${row['captured_at']}') != null &&
            num.tryParse('${row[key]}') != null)
          FlSpot(
            DateTime.parse(
              '${row['captured_at']}',
            ).millisecondsSinceEpoch.toDouble(),
            num.parse('${row[key]}').toDouble(),
          ),
    ]..sort((a, b) => a.x.compareTo(b.x));
    final cpu = points('cpu_percent');
    final ram = points('memory_percent');
    final all = [...cpu, ...ram];
    if (all.isEmpty) return const Text('لا توجد قياسات موارد صالحة للرسم.');
    final minX = all.map((e) => e.x).reduce((a, b) => a < b ? a : b);
    var maxX = all.map((e) => e.x).reduce((a, b) => a > b ? a : b);
    if (minX == maxX) maxX += 60000;
    List<FlSpot> withGaps(List<FlSpot> values) => [
      for (var index = 0; index < values.length; index++) ...[
        if (index > 0 &&
            values[index].x - values[index - 1].x > data.bucketMinutes * 120000)
          FlSpot.nullSpot,
        values[index],
      ],
    ];
    return SizedBox(
      height: 225,
      child: Directionality(
        textDirection: TextDirection.ltr,
        child: LineChart(
          LineChartData(
            minY: 0,
            maxY: 100,
            minX: minX,
            maxX: maxX,
            borderData: FlBorderData(show: false),
            gridData: const FlGridData(drawVerticalLine: false),
            titlesData: FlTitlesData(
              topTitles: const AxisTitles(
                sideTitles: SideTitles(showTitles: false),
              ),
              rightTitles: const AxisTitles(
                sideTitles: SideTitles(showTitles: false),
              ),
              leftTitles: const AxisTitles(
                sideTitles: SideTitles(
                  showTitles: true,
                  reservedSize: 35,
                  interval: 25,
                ),
              ),
              bottomTitles: AxisTitles(
                sideTitles: SideTitles(
                  showTitles: true,
                  reservedSize: 30,
                  interval: (maxX - minX) / 3,
                  minIncluded: false,
                  maxIncluded: false,
                  getTitlesWidget: (value, meta) => Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      DateFormat(
                        data.window == '7d' ? 'MM/dd' : 'HH:mm',
                      ).format(
                        DateTime.fromMillisecondsSinceEpoch(value.toInt()),
                      ),
                      style: const TextStyle(fontSize: 10),
                    ),
                  ),
                ),
              ),
            ),
            lineTouchData: LineTouchData(
              touchTooltipData: LineTouchTooltipData(
                getTooltipItems: (spots) => spots
                    .map(
                      (spot) => LineTooltipItem(
                        '${DateFormat('MM/dd HH:mm').format(DateTime.fromMillisecondsSinceEpoch(spot.x.toInt()))}\n${spot.barIndex == 0 ? 'CPU' : 'RAM'} ${spot.y.toStringAsFixed(1)}%',
                        const TextStyle(color: Colors.white),
                      ),
                    )
                    .toList(),
              ),
            ),
            lineBarsData: [
              for (final pair in [
                (cpu, const Color(0xFF356AA0)),
                (ram, const Color(0xFF138A4B)),
              ])
                LineChartBarData(
                  spots: withGaps(pair.$1),
                  color: pair.$2,
                  barWidth: 2,
                  dotData: FlDotData(
                    show: true,
                    getDotPainter: (spot, percent, bar, index) =>
                        FlDotCirclePainter(
                          radius: 1.8,
                          color: pair.$2,
                          strokeWidth: 0,
                        ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
