import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../design_system.dart';
import '../models.dart';
import '../state.dart';
import '../widgets/status_widgets.dart';
import 'project_logs_screen.dart';
import 'analytics_panel.dart';
import 'action_history_screen.dart';

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
    final dashboard = ref.watch(dashboardProvider).asData?.value;
    final agentReady = dashboard?.agentReady == true;
    final canRunActions = dashboard?.currentUser.can('run_actions') == true;
    final canViewLogs = dashboard?.currentUser.can('view_logs') == true;
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
                      onCheck: dashboard?.currentUser.can('run_checks') == true
                          ? () => _runCheck(context, ref, data.project)
                          : null,
                    ),
                    const SizedBox(height: 14),
                    AnalyticsPanel(id: projectId),
                    const SizedBox(height: 14),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        OutlinedButton.icon(
                          onPressed: () => Navigator.of(context).push(
                            MaterialPageRoute(
                              builder: (_) =>
                                  ActionHistoryScreen(project: data.project),
                            ),
                          ),
                          icon: const Icon(Icons.history),
                          label: const Text('سجل الإجراءات ونتائجها'),
                        ),
                        if (dashboard?.currentUser.can('run_checks') == true)
                          OutlinedButton.icon(
                            onPressed: agentReady
                                ? () => _runDiagnostics(
                                    context,
                                    ref,
                                    data.project,
                                  )
                                : null,
                            icon: const Icon(Icons.troubleshoot),
                            label: const Text('تشخيص المشروع'),
                          ),
                      ],
                    ),
                    const SizedBox(height: 14),
                    _ServicesPanel(
                      project: data.project,
                      canRestart: agentReady && canRunActions,
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
                    if (canViewLogs)
                      Card(
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              const Text(
                                'سجلات الخدمات',
                                style: TextStyle(fontWeight: FontWeight.w900),
                              ),
                              const SizedBox(height: 8),
                              const Text(
                                'قراءة محددة للخدمة والوقت، مع تصنيف الأخطاء والتحذيرات.',
                              ),
                              const SizedBox(height: 8),
                              OutlinedButton.icon(
                                onPressed: agentReady
                                    ? () => Navigator.of(context).push(
                                        MaterialPageRoute(
                                          builder: (_) => ProjectLogsScreen(
                                            project: data.project,
                                          ),
                                        ),
                                      )
                                    : null,
                                icon: const Icon(Icons.receipt_long_outlined),
                                label: const Text('قراءة وتحليل السجلات'),
                              ),
                            ],
                          ),
                        ),
                      ),
                    if (!agentReady)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 12),
                        child: Text(
                          'وكيل الخادم غير متصل؛ إجراءات الخدمات والسجلات معطلة.',
                        ),
                      ),
                    if (data.project.backupAllowed)
                      Card(
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Row(
                            children: [
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    const Text(
                                      'نسخة احتياطية فورية',
                                      style: TextStyle(
                                        fontWeight: FontWeight.w900,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      data.project.backupDescription.isEmpty
                                          ? 'ينفذ مسار النسخ المعتمد لهذا المشروع عبر وكيل العمليات.'
                                          : data.project.backupDescription,
                                      style: TextStyle(
                                        color: context.ops.slate,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              OutlinedButton.icon(
                                onPressed: agentReady && canRunActions
                                    ? () => _confirmAction(
                                        context,
                                        ref,
                                        data.project,
                                        'create_backup',
                                      )
                                    : null,
                                icon: const Icon(Icons.backup_outlined),
                                label: const Text('إنشاء نسخة'),
                              ),
                            ],
                          ),
                        ),
                      ),
                    if (data.project.slug == 'tawtheeq' &&
                        dashboard?.currentUser.can('manage_team') == true)
                      Card(
                        child: ListTile(
                          title: const Text('إعادة تحميل Caddy'),
                          subtitle: const Text(
                            'يتحقق من إعداد الوكيل المشترك ثم يعيد تحميله؛ قد يؤثر في كل مشاريع الخادم.',
                          ),
                          trailing: OutlinedButton(
                            onPressed: agentReady
                                ? () => _confirmAction(
                                    context,
                                    ref,
                                    data.project,
                                    'reload_proxy',
                                  )
                                : null,
                            child: const Text('إعادة تحميل'),
                          ),
                        ),
                      ),
                    if (data.actions.isNotEmpty) ...[
                      const SizedBox(height: 14),
                      Card(
                        child: Column(
                          children: [
                            const ListTile(title: Text('الإجراءات الأخيرة')),
                            for (final action in data.actions.take(8))
                              ListTile(
                                dense: true,
                                title: Text(action.actionLabel),
                                subtitle: Text(action.resultSummary),
                                trailing: Text(action.statusLabel),
                                onTap: () => Navigator.of(context).push(
                                  MaterialPageRoute(
                                    builder: (_) => ActionDetailScreen(
                                      projectId: projectId,
                                      initialAction: action,
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
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

  Future<void> _runDiagnostics(
    BuildContext context,
    WidgetRef ref,
    ProjectInfo project,
  ) async {
    try {
      final result = await ref
          .read(apiProvider)
          .runAction(project.id, 'diagnostics');
      if (context.mounted) {
        await Navigator.of(context).push(
          MaterialPageRoute(
            builder: (_) => ActionDetailScreen(
              projectId: project.id,
              initialAction: result,
            ),
          ),
        );
      }
      ref.invalidate(projectProvider(project.id));
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
              : action == 'reload_proxy'
              ? 'إعادة تحميل الوكيل المشترك'
              : 'إعادة تشغيل ${service?.name ?? ''}',
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              action == 'reload_proxy'
                  ? 'هذا الوكيل يخدم كل المشاريع على الخادم. سيُسجل الإجراء باسم حسابك. اكتب معرف المشروع للتأكيد:'
                  : 'سيُسجل هذا الإجراء باسم حسابك. اكتب معرف المشروع للتأكيد:',
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
      final result = await ref
          .read(apiProvider)
          .runAction(
            project.id,
            action,
            serviceId: service?.id,
            confirmation: project.slug,
          );
      ref.invalidate(projectProvider(project.id));
      if (context.mounted) {
        await Navigator.of(context).push(
          MaterialPageRoute(
            builder: (_) => ActionDetailScreen(
              projectId: project.id,
              initialAction: result,
            ),
          ),
        );
        ref.invalidate(projectProvider(project.id));
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
  final VoidCallback? onCheck;
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
                  style: TextStyle(color: context.ops.slate),
                ),
                if (project.lastCheckedAt != null)
                  Text(
                    'آخر فحص ${DateFormat('yyyy/MM/dd HH:mm').format(project.lastCheckedAt!)}',
                    style: TextStyle(color: context.ops.muted, fontSize: 12),
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

class _ServicesPanel extends StatelessWidget {
  const _ServicesPanel({
    required this.project,
    required this.onRestart,
    required this.canRestart,
  });
  final ProjectInfo project;
  final ValueChanged<ServiceInfo> onRestart;
  final bool canRestart;
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
              trailing: service.restartAllowed && canRestart
                  ? IconButton(
                      tooltip: 'إعادة تشغيل الخدمة',
                      onPressed: () => onRestart(service),
                      icon: const Icon(Icons.restart_alt),
                    )
                  : Icon(
                      Icons.lock_outline,
                      size: 20,
                      color: context.ops.muted,
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
                    color: check.ok ? context.ops.healthy : context.ops.danger,
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
