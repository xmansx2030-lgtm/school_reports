import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api_client.dart';
import '../models.dart';
import '../state.dart';

class ProjectLogsScreen extends ConsumerStatefulWidget {
  const ProjectLogsScreen({super.key, required this.project});
  final ProjectInfo project;

  @override
  ConsumerState<ProjectLogsScreen> createState() => _ProjectLogsScreenState();
}

class _ProjectLogsScreenState extends ConsumerState<ProjectLogsScreen> {
  int? _serviceId;
  int _sinceMinutes = 30;
  bool _loading = false;
  OperationActionInfo? _capture;
  String? _error;
  String _filter = '';

  Future<void> _refreshCapture() async {
    final id = _capture?.id;
    if (id == null || _loading) return;
    setState(() => _loading = true);
    try {
      final current = await ref
          .read(apiProvider)
          .actionDetail(widget.project.id, id);
      if (mounted) {
        setState(() {
          _capture = current;
          _error = null;
        });
      }
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _load() async {
    if (_loading || _serviceId == null) return;
    setState(() {
      _loading = true;
      _error = null;
      _capture = null;
    });
    try {
      final requested = await ref
          .read(apiProvider)
          .runAction(
            widget.project.id,
            'read_logs',
            serviceId: _serviceId,
            sinceMinutes: _sinceMinutes,
            tail: 250,
          );
      if (!mounted) return;
      setState(() => _capture = requested);
      for (var attempt = 0; attempt < 35 && mounted; attempt++) {
        await Future<void>.delayed(const Duration(seconds: 2));
        if (!mounted) return;
        final current = await ref
            .read(apiProvider)
            .actionDetail(widget.project.id, requested.id);
        if (!mounted) return;
        setState(() => _capture = current);
        if (current.status == 'succeeded' || current.status == 'failed') break;
      }
      if (_capture?.status == 'queued' || _capture?.status == 'running') {
        setState(
          () => _error = 'ما زال وكيل الخادم يعمل. أعد تحميل النتيجة بعد قليل.',
        );
      }
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final content = _capture?.logContent ?? '';
    final lines = _filter.trim().isEmpty
        ? content
        : content
              .split('\n')
              .where(
                (line) => line.toLowerCase().contains(_filter.toLowerCase()),
              )
              .join('\n');
    final analysis = _capture?.logAnalysis ?? const <String, dynamic>{};
    final categories = Map<String, dynamic>.from(
      analysis['categories'] as Map? ?? const {},
    );
    final findings = analysis['findings'] as List? ?? const [];
    return Scaffold(
      appBar: AppBar(title: Text('سجلات ${widget.project.name}')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'تعرض هذه الصفحة آخر أسطر Docker للخدمة المحددة بعد حجب أنماط بيانات الاعتماد. التحليل محلي وقائم على تصنيف الأخطاء الظاهرة.',
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<int>(
            initialValue: _serviceId,
            decoration: const InputDecoration(labelText: 'الخدمة'),
            items: widget.project.services
                .map(
                  (service) => DropdownMenuItem<int>(
                    value: service.id,
                    child: Text(service.name),
                  ),
                )
                .toList(),
            onChanged: _loading
                ? null
                : (value) => setState(() => _serviceId = value),
          ),
          const SizedBox(height: 10),
          DropdownButtonFormField<int>(
            initialValue: _sinceMinutes,
            decoration: const InputDecoration(labelText: 'الفترة'),
            items: const [
              DropdownMenuItem(value: 5, child: Text('آخر 5 دقائق')),
              DropdownMenuItem(value: 30, child: Text('آخر 30 دقيقة')),
              DropdownMenuItem(value: 60, child: Text('آخر ساعة')),
              DropdownMenuItem(value: 180, child: Text('آخر 3 ساعات')),
            ],
            onChanged: _loading
                ? null
                : (value) => setState(() => _sinceMinutes = value ?? 30),
          ),
          const SizedBox(height: 12),
          FilledButton.icon(
            onPressed: _loading || _serviceId == null ? null : _load,
            icon: const Icon(Icons.manage_search),
            label: const Text('قراءة وتحليل السجلات'),
          ),
          if (_loading) const LinearProgressIndicator(),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 12),
              child: Text(_error!, style: const TextStyle(color: Colors.red)),
            ),
          if (_capture != null) ...[
            const SizedBox(height: 12),
            if (_capture!.status == 'queued' || _capture!.status == 'running')
              OutlinedButton.icon(
                onPressed: _loading ? null : _refreshCapture,
                icon: const Icon(Icons.refresh),
                label: const Text('تحديث نتيجة الطلب الحالي'),
              ),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'الحالة: ${_capture!.status} · ${_capture!.resultSummary}',
                    ),
                    Text(
                      'الأسطر: ${analysis['lines'] ?? 0} · '
                      'الأخطاء: ${analysis['errors'] ?? 0} · '
                      'التحذيرات: ${analysis['warnings'] ?? 0}',
                    ),
                    if (categories.isNotEmpty)
                      Text(
                        'المؤشرات: ${categories.entries.map((entry) => '${entry.key}: ${entry.value}').join(' · ')}',
                      ),
                    for (final item in findings)
                      if (item is Map)
                        Padding(
                          padding: const EdgeInsets.only(top: 6),
                          child: Text(
                            '${item['category']} (${item['count']}): ${item['next_check']}',
                          ),
                        ),
                    if (analysis['truncated'] == true)
                      const Text('اقتُطع العرض عند حد الأمان.'),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              onChanged: (value) => setState(() => _filter = value),
              decoration: const InputDecoration(
                labelText: 'تصفية الأسطر المعروضة',
                prefixIcon: Icon(Icons.search),
              ),
            ),
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: SelectableText(
                  lines.isEmpty
                      ? 'لا توجد أسطر ضمن الفترة أو التصفية المحددة.'
                      : lines,
                  textDirection: TextDirection.ltr,
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
