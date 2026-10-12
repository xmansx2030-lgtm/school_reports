import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../models.dart';
import '../state.dart';

class ActionHistoryScreen extends ConsumerStatefulWidget {
  const ActionHistoryScreen({super.key, required this.project});
  final ProjectInfo project;
  @override
  ConsumerState<ActionHistoryScreen> createState() =>
      _ActionHistoryScreenState();
}

class _ActionHistoryScreenState extends ConsumerState<ActionHistoryScreen> {
  final List<OperationActionInfo> _actions = [];
  String _status = '';
  int? _next;
  bool _loading = false;
  String? _error;
  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load({bool more = false}) async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final page = await ref
          .read(apiProvider)
          .actionHistory(
            widget.project.id,
            status: _status,
            beforeId: more ? _next : null,
          );
      if (mounted) {
        setState(() {
          if (!more) _actions.clear();
          _actions.addAll(page.actions);
          _next = page.nextBeforeId;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiException
              ? error.message
              : 'تعذر تحميل الإجراءات.',
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text('إجراءات ${widget.project.name}'),
      actions: [
        IconButton(
          onPressed: _loading ? null : _load,
          icon: const Icon(Icons.refresh),
          tooltip: 'تحديث الإجراءات',
        ),
      ],
    ),
    body: ListView(
      key: const PageStorageKey('actions'),
      padding: const EdgeInsets.all(16),
      children: [
        DropdownButtonFormField<String>(
          initialValue: _status,
          decoration: const InputDecoration(labelText: 'حالة الإجراء'),
          items: [
            const DropdownMenuItem(value: '', child: Text('كل الحالات')),
            for (final status in ['queued', 'running', 'succeeded', 'failed'])
              DropdownMenuItem(
                value: status,
                child: Text(actionStatusLabel(status)),
              ),
          ],
          onChanged: _loading
              ? null
              : (value) {
                  setState(() => _status = value ?? '');
                  _load();
                },
        ),
        if (_loading) const LinearProgressIndicator(),
        if (_error != null)
          Padding(padding: const EdgeInsets.all(12), child: Text(_error!)),
        if (!_loading && _actions.isEmpty && _error == null)
          const Padding(
            padding: EdgeInsets.all(24),
            child: Text('لا توجد إجراءات ضمن التصفية المحددة.'),
          ),
        for (final action in _actions)
          Card(
            child: ListTile(
              title: Text('${action.actionLabel} · #${action.id}'),
              subtitle: Text(
                '${action.statusLabel}${action.serviceName.isEmpty ? '' : ' · ${action.serviceName}'}\n${_date(action.requestedAt)} · ${action.requestedByName}\n${action.resultSummary}',
              ),
              trailing: const Icon(Icons.chevron_left),
              onTap: () async {
                await Navigator.of(context).push(
                  MaterialPageRoute(
                    builder: (_) => ActionDetailScreen(
                      projectId: widget.project.id,
                      initialAction: action,
                    ),
                  ),
                );
                if (mounted) _load();
              },
            ),
          ),
        if (_next != null)
          OutlinedButton(
            onPressed: _loading ? null : () => _load(more: true),
            child: const Text('تحميل إجراءات أقدم'),
          ),
      ],
    ),
  );
}

class ActionDetailScreen extends ConsumerStatefulWidget {
  const ActionDetailScreen({
    super.key,
    required this.projectId,
    required this.initialAction,
  });
  final int projectId;
  final OperationActionInfo initialAction;
  @override
  ConsumerState<ActionDetailScreen> createState() => _ActionDetailScreenState();
}

class _ActionDetailScreenState extends ConsumerState<ActionDetailScreen> {
  late OperationActionInfo _action;
  Timer? _timer;
  bool _loading = false;
  String? _error;
  int _attempts = 0;
  @override
  void initState() {
    super.initState();
    _action = widget.initialAction;
    _refresh();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh({bool manual = false}) async {
    if (_loading) return;
    if (manual) _attempts = 0;
    _timer?.cancel();
    setState(() => _loading = true);
    try {
      final action = await ref
          .read(apiProvider)
          .actionDetail(widget.projectId, _action.id);
      if (mounted) {
        setState(() {
          _action = action;
          _error = null;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = error is ApiException
              ? error.message
              : 'تعذر تحديث نتيجة الإجراء.',
        );
      }
    } finally {
      if (mounted) {
        setState(() => _loading = false);
        _attempts++;
        if (!_action.finished && _attempts < 60) {
          _timer = Timer(const Duration(seconds: 3), _refresh);
        }
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text('الإجراء #${_action.id}'),
      actions: [
        IconButton(
          onPressed: _loading ? null : () => _refresh(manual: true),
          icon: const Icon(Icons.refresh),
          tooltip: 'تحديث النتيجة',
        ),
      ],
    ),
    body: ListView(
      key: const PageStorageKey('action-detail'),
      padding: const EdgeInsets.all(16),
      children: [
        Text(
          _action.actionLabel,
          style: Theme.of(context).textTheme.titleLarge,
        ),
        const SizedBox(height: 12),
        Text(
          _action.statusLabel,
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: _action.status == 'failed'
                ? Theme.of(context).colorScheme.error
                : null,
          ),
        ),
        if (_loading) const LinearProgressIndicator(),
        if (!_action.finished)
          Text(
            _attempts >= 60
                ? 'الإجراء ما زال قيد التنفيذ؛ استخدم تحديث النتيجة لمتابعته. لن يُرسل طلب تنفيذ جديد.'
                : 'تُحدّث نتيجة هذا الإجراء تلقائيًا حتى اكتماله.',
          ),
        if (_error != null)
          Text(
            _error!,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
        const SizedBox(height: 12),
        SelectableText(
          _action.resultSummary.isEmpty
              ? 'لم تصل نتيجة من الوكيل بعد.'
              : _action.resultSummary,
        ),
        if (_action.errorCode.isNotEmpty)
          Text('رمز الخطأ: ${_action.errorCode}'),
        if (_action.serviceName.isNotEmpty)
          Text('الخدمة: ${_action.serviceName}'),
        Text(
          'طلبه: ${_action.requestedByName.isEmpty ? 'غير محدد' : _action.requestedByName}',
        ),
        Text('الطلب: ${_date(_action.requestedAt)}'),
        Text('البدء: ${_date(_action.startedAt)}'),
        Text('الانتهاء: ${_date(_action.finishedAt)}'),
        SelectableText('معرف التتبع: ${_action.requestId}'),
        if (_action.diagnostics.isNotEmpty) ...[
          const Divider(height: 28),
          Text('نتائج التشخيص', style: Theme.of(context).textTheme.titleMedium),
          _DiagnosticsResult(data: _action.diagnostics),
        ],
        if (_action.logContent.isNotEmpty) ...[
          const Divider(height: 28),
          OutlinedButton.icon(
            onPressed: () =>
                Clipboard.setData(ClipboardData(text: _action.logContent)),
            icon: const Icon(Icons.copy),
            label: const Text('نسخ السجلات المحجوبة'),
          ),
          SelectableText(_action.logContent, textDirection: TextDirection.ltr),
        ],
      ],
    ),
  );
}

String _date(DateTime? value) =>
    value == null ? '—' : DateFormat('yyyy/MM/dd HH:mm:ss').format(value);

class _DiagnosticsResult extends StatelessWidget {
  const _DiagnosticsResult({required this.data});
  final Map<String, dynamic> data;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      const SizedBox(height: 10),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          Chip(label: Text('عاملة: ${data['running'] ?? '—'}')),
          Chip(label: Text('غير سليمة: ${data['unhealthy'] ?? '—'}')),
          Chip(label: Text('نفاد الذاكرة: ${data['oom_count'] ?? '—'}')),
          Chip(label: Text('إعادات التشغيل: ${data['restart_count'] ?? '—'}')),
        ],
      ),
      for (final row
          in data['services'] is List ? data['services'] as List : const [])
        if (row is Map)
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    '${row['name'] ?? row['service'] ?? ''}',
                    textDirection: TextDirection.ltr,
                    style: const TextStyle(fontWeight: FontWeight.bold),
                  ),
                  Text(
                    'الحالة: ${row['state'] ?? '—'} · الصحة: ${'${row['health'] ?? ''}'.isEmpty ? 'غير محددة' : row['health']}',
                  ),
                  Text(
                    'إعادات التشغيل: ${row['restart_count'] ?? '—'} · رمز الخروج: ${row['exit_code'] ?? '—'}',
                  ),
                  if (row['oom_killed'] == true)
                    Text(
                      'رُصد توقف بسبب نفاد الذاكرة',
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  if (row['resources'] is Map) ...[
                    Text(
                      'CPU ${row['resources']['CPUPerc'] ?? '—'} · RAM ${row['resources']['MemUsage'] ?? '—'}',
                      textDirection: TextDirection.ltr,
                    ),
                    Text(
                      'الشبكة ${row['resources']['NetIO'] ?? '—'} · القرص ${row['resources']['BlockIO'] ?? '—'}',
                    ),
                  ],
                  Text(
                    'تدوير السجل: ${row['log_driver'] ?? '—'} · ${row['log_rotation'] ?? '—'}',
                  ),
                ],
              ),
            ),
          ),
      if (data['backups'] is List && (data['backups'] as List).isNotEmpty) ...[
        const SizedBox(height: 12),
        Text(
          'حالة مهام النسخ الاحتياطية',
          style: Theme.of(context).textTheme.titleMedium,
        ),
        for (final row in data['backups'] as List)
          if (row is Map)
            Card(
              child: ListTile(
                title: Text(
                  '${row['unit'] ?? ''}',
                  textDirection: TextDirection.ltr,
                ),
                subtitle: Text(
                  'الحالة: ${row['ActiveState'] ?? '—'} · نتيجة آخر تشغيل: ${row['Result'] ?? '—'}\nآخر انتهاء: ${row['ExecMainExitTimestamp'] ?? '—'}',
                ),
              ),
            ),
      ],
      ExpansionTile(
        title: const Text('التفاصيل التقنية'),
        children: [
          SelectableText(
            const JsonEncoder.withIndent('  ').convert(data),
            textDirection: TextDirection.ltr,
          ),
        ],
      ),
    ],
  );
}
