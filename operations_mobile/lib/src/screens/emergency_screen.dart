import 'package:flutter/material.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../models.dart';

class EmergencyScreen extends StatefulWidget {
  const EmergencyScreen({super.key});

  @override
  State<EmergencyScreen> createState() => _EmergencyScreenState();
}

class _EmergencyScreenState extends State<EmergencyScreen> {
  final EmergencyApi _api = EmergencyApi();
  final TextEditingController _token = TextEditingController();
  bool _hasToken = false;
  bool _busy = false;
  Map<String, dynamic>? _server;
  int? _pendingActionId;
  String? _error;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _restore();
  }

  @override
  void dispose() {
    _token.dispose();
    super.dispose();
  }

  Future<void> _restore() async {
    final present = await _api.hasToken();
    if (!mounted) return;
    setState(() => _hasToken = present);
    if (present) await _refresh();
  }

  Future<void> _save() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await _api.saveToken(_token.text);
      final status = await _api.overviewData();
      _token.clear();
      if (mounted) {
        setState(() {
          _hasToken = true;
          _server = status;
        });
      }
    } on ApiException catch (error) {
      await _api.clearToken();
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _refresh() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final status = await _api.overviewData();
      if (mounted) setState(() => _server = status);
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _act(String action) async {
    if (_pendingActionId != null) {
      setState(
        () => _notice = 'تحقق من نتيجة الإجراء السابق قبل إرسال إجراء جديد.',
      );
      return;
    }
    final name = _server?['name']?.toString() ?? '';
    final expected = '$name:$action';
    final controller = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(switch (action) {
          'poweron' => 'تشغيل الخادم',
          'reboot' => 'إعادة تشغيل الخادم',
          'shutdown' => 'إيقاف الخادم',
          'snapshot' => 'إنشاء Snapshot',
          _ => action,
        }),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              action == 'snapshot'
                  ? 'سينشئ هذا نسخة قرص مدفوعة. لا تشمل وحدات التخزين الملحقة ولا تحل محل نسخ قواعد البيانات والوسائط.'
                  : 'سيؤثر الإجراء في جميع المشاريع على الخادم. اكتب النص للتأكيد:',
            ),
            SelectableText(expected, textDirection: TextDirection.ltr),
            TextField(
              controller: controller,
              textDirection: TextDirection.ltr,
              decoration: const InputDecoration(labelText: 'نص التأكيد'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('إلغاء'),
          ),
          FilledButton(
            onPressed: () =>
                Navigator.pop(dialogContext, controller.text == expected),
            child: const Text('تنفيذ'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (confirmed != true || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await _api.action(action, expected);
      final actionId = (result['provider_action_id'] as num?)?.toInt();
      if (mounted) {
        setState(() {
          _pendingActionId = actionId;
          _notice =
              'قبلت Hetzner الإجراء #${actionId ?? '—'}. '
              'تحقق من نتيجته قبل طلب إجراء آخر.';
        });
      }
      if (actionId != null) {
        await Future<void>.delayed(const Duration(seconds: 2));
        if (mounted) await _checkPending(showBusy: false);
      }
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _checkPending({bool showBusy = true}) async {
    final id = _pendingActionId;
    if (id == null) return;
    if (showBusy) setState(() => _busy = true);
    try {
      final result = await _api.actionStatus(id);
      if (!mounted) return;
      final status = result['status']?.toString() ?? 'unknown';
      setState(() {
        if (status == 'success' || status == 'error') {
          _pendingActionId = null;
          _notice = status == 'success'
              ? 'اكتمل إجراء Hetzner #$id بنجاح.'
              : 'فشل إجراء Hetzner #$id (${result['error_code'] ?? 'غير معروف'}).';
        } else {
          _notice = 'إجراء Hetzner #$id ما زال بحالة $status.';
        }
      });
    } on ApiException catch (error) {
      if (mounted) {
        setState(
          () => _notice =
              'قُبل الإجراء #$id، لكن تعذر التحقق من نهايته الآن: ${error.message}',
        );
      }
    } finally {
      if (showBusy && mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final details = _server == null
        ? null
        : ProviderServerInfo.fromJson(_server!);
    return Scaffold(
      appBar: AppBar(title: const Text('طوارئ الخادم')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'يتصل هذا المسار مباشرة بخدمة تحكم مستقلة وHetzner عندما يتعذر الوصول إلى مركز العمليات. لا يعتمد على تشغيل Django أو Docker.',
          ),
          const SizedBox(height: 16),
          if (!_hasToken) ...[
            TextField(
              controller: _token,
              obscureText: true,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: 'مفتاح الطوارئ الخاص بجهازك',
              ),
            ),
            const SizedBox(height: 10),
            FilledButton(
              onPressed: _busy ? null : _save,
              child: const Text('حفظ المفتاح والتحقق'),
            ),
          ] else ...[
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('الخادم: ${_server?['name'] ?? '—'}'),
                    Text('حالة Hetzner: ${_server?['status'] ?? 'غير معروفة'}'),
                    if (details != null)
                      Text(
                        'CPU: ${details.latestMetric('cpu')?.toStringAsFixed(1) ?? '—'}% '
                        '· ${details.serverType} · ${details.location}',
                      ),
                    if (details != null)
                      Text('نسخ Hetzner المعروضة: ${details.backups.length}'),
                    if (details?.partialErrors.isNotEmpty == true)
                      Text('قراءة جزئية: ${details!.partialErrors.join('، ')}'),
                    if (_server?['fetched_at'] != null)
                      Text(
                        'آخر قراءة: ${DateFormat('yyyy/MM/dd HH:mm').format(DateTime.parse(_server!['fetched_at'].toString()).toLocal())}',
                      ),
                    const SizedBox(height: 8),
                    OutlinedButton.icon(
                      onPressed: _busy ? null : _refresh,
                      icon: const Icon(Icons.refresh),
                      label: const Text('تحديث الحالة'),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            if (details != null && details.backups.isNotEmpty)
              Card(
                child: Column(
                  children: [
                    const ListTile(title: Text('آخر نسخ Hetzner')),
                    for (final backup in details.backups.take(7))
                      ListTile(
                        dense: true,
                        title: Text('${backup['description'] ?? 'Backup'}'),
                        subtitle: Text('${backup['created'] ?? ''}'),
                      ),
                  ],
                ),
              ),
            if (_server?['status'] == 'off')
              FilledButton(
                onPressed: _busy ? null : () => _act('poweron'),
                child: const Text('تشغيل الخادم'),
              ),
            if (_server?['status'] == 'running') ...[
              OutlinedButton(
                onPressed: _busy ? null : () => _act('reboot'),
                child: const Text('إعادة تشغيل الخادم'),
              ),
              OutlinedButton(
                onPressed: _busy ? null : () => _act('snapshot'),
                child: const Text('إنشاء Snapshot'),
              ),
              OutlinedButton(
                onPressed: _busy ? null : () => _act('shutdown'),
                child: const Text('إيقاف الخادم'),
              ),
            ],
            if (_pendingActionId != null)
              OutlinedButton.icon(
                onPressed: _busy ? null : _checkPending,
                icon: const Icon(Icons.fact_check_outlined),
                label: const Text('التحقق من نتيجة الإجراء'),
              ),
            TextButton(
              onPressed: _busy
                  ? null
                  : () async {
                      await _api.clearToken();
                      if (mounted) {
                        setState(() {
                          _hasToken = false;
                          _server = null;
                          _pendingActionId = null;
                        });
                      }
                    },
              child: const Text('إزالة مفتاح الطوارئ من هذا الجهاز'),
            ),
          ],
          if (_busy) const LinearProgressIndicator(),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(_error!, style: const TextStyle(color: Colors.red)),
            ),
          if (_notice != null)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(_notice!),
            ),
        ],
      ),
    );
  }
}
