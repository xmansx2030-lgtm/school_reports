import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../config.dart';
import '../models.dart';
import '../state.dart';
import 'emergency_screen.dart';

class ServerManagementScreen extends ConsumerStatefulWidget {
  const ServerManagementScreen({super.key, required this.server});
  final ServerInfo server;

  @override
  ConsumerState<ServerManagementScreen> createState() =>
      _ServerManagementScreenState();
}

class _ServerManagementScreenState
    extends ConsumerState<ServerManagementScreen> {
  late Future<ProviderOverview> _overview;
  bool _busy = false;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _overview = _loadOverview();
  }

  Future<ProviderOverview> _loadOverview() async {
    final emergency = EmergencyApi();
    try {
      final provider = await ref
          .read(apiProvider)
          .providerOverview(widget.server.id);
      if (provider.configured ||
          AppConfig.emergencyUrl.isEmpty ||
          !(await emergency.hasToken())) {
        return provider;
      }
    } on ApiException {
      if (AppConfig.emergencyUrl.isEmpty || !(await emergency.hasToken())) {
        rethrow;
      }
    }
    return emergency.overview();
  }

  void _refresh() {
    setState(() {
      _overview = _loadOverview();
    });
  }

  Future<void> _run(String action) async {
    if (_busy) return;
    final requiredText = '${widget.server.slug}:$action';
    final controller = TextEditingController();
    final accepted = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(_actionTitle(action)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              action == 'snapshot'
                  ? 'ينشئ Snapshot مدفوعة للخادم بالكامل. لا تحل محل نسخ قواعد البيانات والوسائط.'
                  : 'سيؤثر هذا الإجراء في كل المشاريع المستضافة على الخادم، وقد ينقطع اتصال التطبيق مؤقتًا.',
            ),
            const SizedBox(height: 12),
            SelectableText(requiredText, textDirection: TextDirection.ltr),
            const SizedBox(height: 8),
            TextField(
              controller: controller,
              textDirection: TextDirection.ltr,
              decoration: const InputDecoration(labelText: 'اكتب النص للتأكيد'),
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
                Navigator.pop(dialogContext, controller.text == requiredText),
            child: const Text('تنفيذ'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (accepted != true || !mounted) return;
    setState(() => _busy = true);
    var submitted = false;
    try {
      final result = await ref
          .read(apiProvider)
          .providerAction(widget.server.id, action, requiredText);
      submitted = true;
      setState(() {
        _notice =
            'قبلت Hetzner طلب ${_actionTitle(action)} (إجراء #${result.id}). '
            'جارٍ التحقق من النتيجة.';
      });
      _refresh();
      for (var attempt = 0; attempt < 8 && mounted; attempt++) {
        await Future<void>.delayed(const Duration(seconds: 2));
        final current = await ref
            .read(apiProvider)
            .providerActionStatus(widget.server.id, result.id);
        if (!mounted) return;
        if (current.finished) {
          setState(
            () => _notice = current.status == 'success'
                ? 'اكتمل إجراء ${_actionTitle(action)} لدى Hetzner.'
                : 'فشل إجراء ${_actionTitle(action)} لدى Hetzner (${current.errorCode}).',
          );
          _refresh();
          return;
        }
      }
      if (mounted) {
        setState(
          () => _notice =
              'ما زال إجراء Hetzner قيد المتابعة، أو تعذر تأكيد نهايته. حدّث حالة الخادم.',
        );
      }
    } on ApiException catch (error) {
      if (mounted) {
        setState(
          () => _notice = submitted
              ? 'قبلت Hetzner الإجراء، لكن تعذر التحقق من نهايته الآن. حدّث الحالة بعد عودة الاتصال.'
              : error.message,
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String _actionTitle(String action) => switch (action) {
    'poweron' => 'تشغيل الخادم',
    'reboot' => 'إعادة تشغيل الخادم',
    'shutdown' => 'إيقاف الخادم بأمان',
    'snapshot' => 'إنشاء Snapshot',
    _ => action,
  };

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text(widget.server.name),
      actions: [
        IconButton(
          onPressed: _refresh,
          tooltip: 'تحديث بيانات Hetzner',
          icon: const Icon(Icons.refresh),
        ),
      ],
    ),
    body: FutureBuilder<ProviderOverview>(
      future: _overview,
      builder: (context, snapshot) {
        if (!snapshot.hasData && !snapshot.hasError) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          return Center(
            child: Text(
              error is ApiException
                  ? error.message
                  : 'تعذر قراءة حالة Hetzner.',
            ),
          );
        }
        final overview = snapshot.data!;
        final provider = overview.server;
        return RefreshIndicator(
          onRefresh: () async {
            _refresh();
            await _overview;
          },
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (_notice != null)
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(14),
                    child: Text(_notice!),
                  ),
                ),
              if (AppConfig.emergencyUrl.isNotEmpty) ...[
                OutlinedButton.icon(
                  onPressed: () async {
                    await Navigator.of(context).push(
                      MaterialPageRoute(
                        builder: (_) => const EmergencyScreen(),
                      ),
                    );
                    if (mounted) _refresh();
                  },
                  icon: const Icon(Icons.power_settings_new),
                  label: const Text('مسار الطوارئ المستقل'),
                ),
                const SizedBox(height: 12),
              ],
              if (provider == null)
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(18),
                    child: Text(
                      overview.detail.isEmpty
                          ? 'بيانات Hetzner غير متاحة.'
                          : overview.detail,
                    ),
                  ),
                )
              else ...[
                if (provider.partialErrors.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      'تعذرت قراءة بعض بيانات Hetzner: ${provider.partialErrors.join('، ')}. حدّث الصفحة لاحقًا.',
                    ),
                  ),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(18),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Hetzner: ${provider.status} · ${provider.serverType} · ${provider.location}',
                          style: const TextStyle(fontWeight: FontWeight.bold),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          'CPU لدى Hetzner: ${provider.latestMetric('cpu')?.toStringAsFixed(1) ?? '—'}% '
                          '· RAM داخل الخادم: ${widget.server.memory?.toStringAsFixed(1) ?? '—'}% '
                          '· القرص داخل الخادم: ${widget.server.disk?.toStringAsFixed(1) ?? '—'}%',
                        ),
                        Text(
                          'حماية الحذف: ${provider.deleteProtected ? 'مفعلة' : 'غير مفعلة'} '
                          '· النسخ التلقائية: ${provider.backupWindow.isEmpty ? 'غير مفعلة' : 'مفعلة'}',
                        ),
                        if (provider.fetchedAt != null)
                          Text(
                            'آخر قراءة من Hetzner: ${DateFormat('yyyy/MM/dd HH:mm').format(provider.fetchedAt!)}',
                          ),
                        if (widget.server.lastCheckedAt != null)
                          Text(
                            'آخر جرد من المضيف: ${DateFormat('yyyy/MM/dd HH:mm').format(widget.server.lastCheckedAt!)}',
                          ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(18),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'إجراءات الخادم',
                          style: TextStyle(fontWeight: FontWeight.bold),
                        ),
                        const SizedBox(height: 8),
                        if (!overview.canControl)
                          const Text(
                            'التحكم عبر Django غير مهيأ لهذا الحساب؛ استخدم مسار الطوارئ المستقل.',
                          ),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            for (final action in [
                              if (provider.status == 'off') 'poweron',
                              if (provider.status == 'running') ...[
                                'reboot',
                                'shutdown',
                              ],
                              'snapshot',
                            ])
                              OutlinedButton(
                                onPressed: overview.canControl && !_busy
                                    ? () => _run(action)
                                    : null,
                                child: Text(_actionTitle(action)),
                              ),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(18),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'نسخ Hetzner الأخيرة',
                          style: TextStyle(fontWeight: FontWeight.bold),
                        ),
                        for (final backup in provider.backups.take(7))
                          ListTile(
                            dense: true,
                            title: Text('${backup['description'] ?? 'Backup'}'),
                            subtitle: Text('${backup['created'] ?? ''}'),
                            trailing: Text('${backup['status'] ?? ''}'),
                          ),
                        if (provider.backups.isEmpty)
                          const Text('لا توجد نسخ معروضة.'),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(18),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'إجراءات Hetzner الأخيرة',
                          style: TextStyle(fontWeight: FontWeight.bold),
                        ),
                        for (final action in provider.recentActions.take(10))
                          ListTile(
                            dense: true,
                            title: Text('${action['command'] ?? ''}'),
                            subtitle: Text('${action['started'] ?? ''}'),
                            trailing: Text('${action['status'] ?? ''}'),
                          ),
                      ],
                    ),
                  ),
                ),
              ],
            ],
          ),
        );
      },
    ),
  );
}
