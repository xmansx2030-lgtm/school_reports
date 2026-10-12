import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
  String _severity = 'all';
  int _tail = 500;
  final List<OperationActionInfo> _pages = [];

  @override
  void initState() {
    super.initState();
    if (widget.project.services.isNotEmpty) {
      _serviceId = widget.project.services.first.id;
    }
  }

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
          if (current.finished &&
              !_pages.any((page) => page.id == current.id)) {
            _pages.add(current);
          }
          _error = null;
        });
      }
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _load({String? until}) async {
    if (_loading || _serviceId == null) return;
    setState(() {
      _loading = true;
      _error = null;
      if (until == null) _pages.clear();
    });
    try {
      final requested = await ref
          .read(apiProvider)
          .runAction(
            widget.project.id,
            'read_logs',
            serviceId: _serviceId,
            sinceMinutes: _sinceMinutes,
            tail: _tail,
            until: until,
            sinceAt: until == null
                ? null
                : _capture?.logPage['since_at']?.toString(),
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
        if (current.finished) {
          setState(() => _pages.add(current));
          break;
        }
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
    final content = _pages.reversed.map((page) => page.logContent).join('\n');
    final lines = content
        .split('\n')
        .where((line) {
          final lower = line.toLowerCase();
          if (_filter.trim().isNotEmpty &&
              !lower.contains(_filter.toLowerCase().trim())) {
            return false;
          }
          return switch (_severity) {
            'error' => RegExp(
              r'error|exception|fatal|critical|traceback|panic',
              caseSensitive: false,
            ).hasMatch(line),
            'warning' => RegExp(
              r'warn|warning',
              caseSensitive: false,
            ).hasMatch(line),
            _ => true,
          };
        })
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
            'اقرأ سجلات خدمة محددة وصفحاتها الأقدم خلال الفترة المختارة. تُحجب أنماط بيانات الاعتماد قبل العرض؛ التحليل يصف الأسطر المسترجعة فقط.',
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<int>(
            isExpanded: true,
            initialValue: _serviceId,
            decoration: const InputDecoration(labelText: 'الخدمة'),
            items: widget.project.services
                .map(
                  (service) => DropdownMenuItem<int>(
                    value: service.id,
                    child: Text(service.name, overflow: TextOverflow.ellipsis),
                  ),
                )
                .toList(),
            onChanged: _loading
                ? null
                : (value) => setState(() {
                    _serviceId = value;
                    _capture = null;
                    _pages.clear();
                  }),
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
              DropdownMenuItem(value: 1440, child: Text('آخر 24 ساعة')),
              DropdownMenuItem(value: 2880, child: Text('آخر 48 ساعة')),
              DropdownMenuItem(value: 10080, child: Text('آخر 7 أيام')),
            ],
            onChanged: _loading
                ? null
                : (value) => setState(() {
                    _sinceMinutes = value ?? 30;
                    _capture = null;
                    _pages.clear();
                  }),
          ),
          const SizedBox(height: 10),
          DropdownButtonFormField<int>(
            initialValue: _tail,
            decoration: const InputDecoration(
              labelText: 'الحد الأقصى لأسطر الصفحة',
            ),
            items: const [
              DropdownMenuItem(value: 250, child: Text('250 سطرًا')),
              DropdownMenuItem(value: 500, child: Text('500 سطر')),
              DropdownMenuItem(value: 1000, child: Text('1000 سطر')),
            ],
            onChanged: _loading
                ? null
                : (value) => setState(() => _tail = value ?? 500),
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
                      'الحالة: ${_capture!.statusLabel} · ${_capture!.resultSummary}',
                    ),
                    Text(
                      'الصفحة الأخيرة · الأسطر: ${analysis['lines'] ?? 0} · '
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
                    if (analysis['truncated'] == true ||
                        _capture!.logPage['truncated'] == true)
                      const Text(
                        'النتيجة مقتطعة عند حد الصفحة أو حد الحماية. هذه ليست كل سجلات الفترة.',
                      ),
                    if (_capture!.logPage['has_more'] == true &&
                        '${_capture!.logPage['next_until'] ?? ''}'.isNotEmpty)
                      OutlinedButton.icon(
                        onPressed: _loading
                            ? null
                            : () => _load(
                                until: '${_capture!.logPage['next_until']}',
                              ),
                        icon: const Icon(Icons.history),
                        label: const Text('تحميل صفحة أقدم'),
                      ),
                    Text('الصفحات المسترجعة: ${_pages.length}'),
                    if ('${_capture!.logPage['retention_note'] ?? ''}'
                        .isNotEmpty)
                      Text('${_capture!.logPage['retention_note']}'),
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
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final item in const {
                  'all': 'كل المستويات',
                  'error': 'أخطاء',
                  'warning': 'تحذيرات',
                }.entries)
                  ChoiceChip(
                    label: Text(item.value),
                    selected: _severity == item.key,
                    onSelected: (_) => setState(() => _severity = item.key),
                  ),
                OutlinedButton.icon(
                  onPressed: lines.isEmpty
                      ? null
                      : () async {
                          await Clipboard.setData(ClipboardData(text: lines));
                          if (context.mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(
                                content: Text('تم نسخ الأسطر المعروضة.'),
                              ),
                            );
                          }
                        },
                  icon: const Icon(Icons.copy),
                  label: const Text('نسخ المعروض'),
                ),
              ],
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
