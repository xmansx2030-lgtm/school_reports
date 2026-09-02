import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart' show DateFormat;

import '../api_client.dart';
import '../design_system.dart';
import '../models.dart';
import '../state.dart';
import '../widgets/status_widgets.dart';
import 'accounts_screen.dart';
import 'change_password_screen.dart';
import 'project_screen.dart';

class DashboardScreen extends ConsumerStatefulWidget {
  const DashboardScreen({super.key});

  @override
  ConsumerState<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends ConsumerState<DashboardScreen> {
  int _index = 0;

  static const _titles = ['نظرة عامة', 'الخوادم', 'التنبيهات', 'الحساب'];
  static const _subtitles = [
    'بيئة الإنتاج · اتصال مباشر',
    'الخوادم والمشاريع المُراقَبة',
    'الحالات التي تحتاج إلى متابعة',
    'حسابك وإعدادات الفريق',
  ];

  @override
  Widget build(BuildContext context) {
    final dashboard = ref.watch(dashboardProvider);
    final data = dashboard.asData?.value;
    final openIncidents = data?.openIncidentCount ?? 0;
    return Scaffold(
      appBar: AppBar(
        toolbarHeight: 76,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _titles[_index],
              style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 21),
            ),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 7,
                  height: 7,
                  decoration: const BoxDecoration(
                    color: Color(0xFF58D68D),
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 6),
                Text(
                  _subtitles[_index],
                  style: const TextStyle(
                    fontSize: 11,
                    color: Color(0xFFB9CAC2),
                  ),
                ),
              ],
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'تحديث الحالة',
            onPressed: () => ref.read(dashboardProvider.notifier).refresh(),
            icon: const Icon(Icons.sync_rounded),
          ),
          const SizedBox(width: 6),
        ],
      ),
      body: dashboard.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => _ErrorView(
          message: error is ApiException
              ? error.message
              : 'تعذر تحميل حالة الخادم.',
          onRetry: () => ref.read(dashboardProvider.notifier).refresh(),
        ),
        data: _tabBody,
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (value) => setState(() => _index = value),
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard_rounded),
            label: 'نظرة عامة',
          ),
          const NavigationDestination(
            icon: Icon(Icons.dns_outlined),
            selectedIcon: Icon(Icons.dns_rounded),
            label: 'الخوادم',
          ),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: openIncidents > 0,
              label: Text('$openIncidents'),
              child: const Icon(Icons.notifications_outlined),
            ),
            selectedIcon: Badge(
              isLabelVisible: openIncidents > 0,
              label: Text('$openIncidents'),
              child: const Icon(Icons.notifications_rounded),
            ),
            label: 'التنبيهات',
          ),
          const NavigationDestination(
            icon: Icon(Icons.person_outline_rounded),
            selectedIcon: Icon(Icons.person_rounded),
            label: 'الحساب',
          ),
        ],
      ),
    );
  }

  Widget _tabBody(DashboardData data) {
    final body = switch (_index) {
      1 => _serversTab(data),
      2 => [_IncidentsPanel(data: data)],
      3 => [_AccountTab(user: data.currentUser)],
      _ => _overviewTab(data),
    };
    return RefreshIndicator(
      onRefresh: () => ref.read(dashboardProvider.notifier).refresh(),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(16),
        children: [
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 900),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: body,
              ),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _overviewTab(DashboardData data) => [
    _Overview(data: data),
    const SizedBox(height: 16),
    _DeploymentPanel(canRunActions: data.currentUser.can('run_actions')),
  ];

  List<Widget> _serversTab(DashboardData data) {
    if (data.servers.isEmpty) {
      return const [
        Card(
          child: EmptyState(
            icon: Icons.dns_outlined,
            title: 'لا يوجد خادم مسجل',
            message: 'أضف جرد الخادم من لوحة Django.',
          ),
        ),
      ];
    }
    return data.servers
        .map(
          (server) => Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: _ServerPanel(server: server),
          ),
        )
        .toList();
  }
}

class _AccountTab extends ConsumerWidget {
  const _AccountTab({required this.user});
  final OperationsAccount user;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final name = user.name.isEmpty ? 'مستخدم مركز العمليات' : user.name;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        PremiumPanel(
          gradient: const LinearGradient(
            begin: Alignment.topRight,
            end: Alignment.bottomLeft,
            colors: [OpsColors.ink, OpsColors.forest],
          ),
          child: Row(
            children: [
              Container(
                width: 56,
                height: 56,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: .12),
                  borderRadius: BorderRadius.circular(18),
                ),
                child: const Icon(
                  Icons.person_rounded,
                  color: Colors.white,
                  size: 30,
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      name,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 18,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      user.roleLabel,
                      style: const TextStyle(color: Color(0xFFC7D8D0)),
                    ),
                    if (user.phone.isNotEmpty)
                      Text(
                        user.phone,
                        textDirection: TextDirection.ltr,
                        style: const TextStyle(
                          color: Color(0xFFC7D8D0),
                          fontSize: 12,
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        Card(
          child: Column(
            children: [
              if (user.can('manage_team'))
                ListTile(
                  leading: Icon(
                    Icons.groups_2_outlined,
                    color: context.ops.forest,
                  ),
                  title: const Text('فريق العمليات'),
                  subtitle: const Text('إدارة الحسابات والصلاحيات'),
                  trailing: const Icon(Icons.chevron_left_rounded),
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => AccountsScreen(
                        canManage: user.can('manage_team'),
                      ),
                    ),
                  ),
                ),
              if (user.can('manage_team')) const Divider(height: 1),
              ListTile(
                leading: Icon(
                  Icons.password_outlined,
                  color: context.ops.forest,
                ),
                title: const Text('تغيير كلمة المرور'),
                trailing: const Icon(Icons.chevron_left_rounded),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(
                    builder: (_) => const ChangePasswordScreen(),
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        _ThemeSelector(
          mode: ref.watch(themeModeProvider),
          onChanged: (mode) =>
              ref.read(themeModeProvider.notifier).setMode(mode),
        ),
        const SizedBox(height: 16),
        OutlinedButton.icon(
          style: OutlinedButton.styleFrom(
            foregroundColor: context.ops.danger,
            side: BorderSide(color: context.ops.danger),
            minimumSize: const Size(48, 52),
          ),
          onPressed: () => _confirmLogout(context, ref),
          icon: const Icon(Icons.logout_rounded),
          label: const Text('تسجيل الخروج'),
        ),
      ],
    );
  }

  Future<void> _confirmLogout(BuildContext context, WidgetRef ref) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('تسجيل الخروج'),
        content: const Text('هل تريد تسجيل الخروج من مركز العمليات؟'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: const Text('إلغاء'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: OpsColors.danger),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: const Text('خروج'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await ref.read(sessionProvider.notifier).signOut();
    }
  }
}

class _ThemeSelector extends StatelessWidget {
  const _ThemeSelector({required this.mode, required this.onChanged});
  final ThemeMode mode;
  final ValueChanged<ThemeMode> onChanged;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(Icons.palette_outlined, color: ops.forest, size: 20),
                const SizedBox(width: 10),
                Text(
                  'مظهر التطبيق',
                  style: TextStyle(
                    fontWeight: FontWeight.w900,
                    color: ops.ink,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            SegmentedButton<ThemeMode>(
              segments: const [
                ButtonSegment(
                  value: ThemeMode.system,
                  label: Text('النظام'),
                  icon: Icon(Icons.brightness_auto_outlined),
                ),
                ButtonSegment(
                  value: ThemeMode.light,
                  label: Text('فاتح'),
                  icon: Icon(Icons.light_mode_outlined),
                ),
                ButtonSegment(
                  value: ThemeMode.dark,
                  label: Text('داكن'),
                  icon: Icon(Icons.dark_mode_outlined),
                ),
              ],
              selected: {mode},
              showSelectedIcon: false,
              onSelectionChanged: (selection) => onChanged(selection.first),
            ),
          ],
        ),
      ),
    );
  }
}

class _Overview extends StatelessWidget {
  const _Overview({required this.data});
  final DashboardData data;
  @override
  Widget build(BuildContext context) {
    final allHealthy =
        data.projectCount > 0 &&
        data.healthyProjectCount == data.projectCount &&
        data.openIncidentCount == 0;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        PremiumPanel(
          padding: const EdgeInsets.all(20),
          gradient: LinearGradient(
            begin: Alignment.topRight,
            end: Alignment.bottomLeft,
            colors: allHealthy
                ? const [OpsColors.ink, OpsColors.forest]
                : const [Color(0xFF2B2A1B), Color(0xFF6A4B15)],
          ),
          child: Row(
            children: [
              Container(
                width: 52,
                height: 52,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: .1),
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Icon(
                  allHealthy
                      ? Icons.check_circle_outline
                      : Icons.warning_amber_rounded,
                  color: allHealthy ? const Color(0xFF58D68D) : OpsColors.gold,
                  size: 30,
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      allHealthy
                          ? 'جميع المشاريع تعمل بصورة طبيعية'
                          : 'توجد حالة تحتاج إلى المتابعة',
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 17,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    if (data.generatedAt != null)
                      Text(
                        'آخر مزامنة ${DateFormat('HH:mm:ss').format(data.generatedAt!)}',
                        style: const TextStyle(color: Color(0xFFC7D8D0)),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 14),
        Row(
          children: [
            Expanded(
              child: _SummaryItem(
                label: 'المشاريع',
                value: '${data.projectCount}',
                icon: Icons.apps_outlined,
                color: context.ops.info,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _SummaryItem(
                label: 'السليمة',
                value: '${data.healthyProjectCount}',
                icon: Icons.health_and_safety_outlined,
                color: context.ops.emerald,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _SummaryItem(
                label: 'الحوادث',
                value: '${data.openIncidentCount}',
                icon: Icons.notification_important_outlined,
                color: data.openIncidentCount > 0
                    ? context.ops.danger
                    : context.ops.slate,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _DeploymentPanel extends ConsumerStatefulWidget {
  const _DeploymentPanel({required this.canRunActions});

  final bool canRunActions;

  @override
  ConsumerState<_DeploymentPanel> createState() => _DeploymentPanelState();
}

class _DeploymentPanelState extends ConsumerState<_DeploymentPanel> {
  final Set<int> _deployingProjects = {};

  @override
  Widget build(BuildContext context) {
    final deployment = ref.watch(deploymentProvider);
    return deployment.when(
      loading: () => const Card(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Row(
            children: [
              SizedBox(
                width: 22,
                height: 22,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              SizedBox(width: 12),
              Text('جاري فحص توافق المستودع مع الخادم...'),
            ],
          ),
        ),
      ),
      error: (error, _) => Card(
        child: EmptyState(
          icon: Icons.hub_outlined,
          title: 'تعذر فحص النشر',
          message: error is ApiException
              ? error.message
              : 'تعذر قراءة حالة GitHub والخادم.',
        ),
      ),
      data: (overview) => PremiumPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SectionHeading(
              icon: overview.repositoryAheadCount > 0
                  ? Icons.rocket_launch_outlined
                  : Icons.verified_outlined,
              title: 'إدارة الإصدارات',
              subtitle: overview.repositoryAheadCount > 0
                  ? '${overview.repositoryAheadCount} مشروع جاهز للمراجعة والنشر'
                  : 'جميع الإصدارات المنشورة متزامنة',
              trailing: IconButton.filledTonal(
                tooltip: 'تحديث حالة النشر',
                onPressed: () => ref.invalidate(deploymentProvider),
                icon: const Icon(Icons.sync_rounded),
              ),
            ),
            const SizedBox(height: 16),
            if (overview.deployments.isEmpty)
              const EmptyState(
                icon: Icons.hub_outlined,
                title: 'لا توجد مستودعات مرتبطة',
                message: 'اربط كل مشروع بمستودعه من إعدادات العمليات.',
              )
            else
              ...overview.deployments.map(
                (info) => _DeploymentRow(
                  info: info,
                  workflowLabel: _workflowLabel(info),
                  isDeploying: _deployingProjects.contains(info.projectId),
                  onDeploy:
                      widget.canRunActions &&
                          info.canDeploy &&
                          !_deployingProjects.contains(info.projectId)
                      ? () => _deployNow(info)
                      : null,
                ),
              ),
          ],
        ),
      ),
    );
  }

  String _workflowLabel(DeploymentInfo info) {
    if (info.workflowStatus == 'in_progress') return 'قيد التنفيذ';
    if (info.workflowConclusion == 'success') return 'نجح';
    if (info.workflowConclusion == 'failure') return 'فشل';
    return info.workflowStatus.isEmpty ? 'غير معروف' : info.workflowStatus;
  }

  Future<void> _deployNow(DeploymentInfo info) async {
    if (_deployingProjects.contains(info.projectId)) return;
    setState(() => _deployingProjects.add(info.projectId));
    try {
      await ref
          .read(apiProvider)
          .triggerDeployment(
            projectId: info.projectId,
            confirmation: info.latestShortSha,
          );
      ref.invalidate(deploymentProvider);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              'بدأ نشر ${info.projectName}. ستتحدث الحالة تلقائياً.',
            ),
            backgroundColor: OpsColors.forest,
          ),
        );
      }
      for (final delay in const [4, 12, 25]) {
        Future<void>.delayed(Duration(seconds: delay), () {
          if (mounted) ref.invalidate(deploymentProvider);
        });
      }
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.message)));
      }
    } finally {
      if (mounted) {
        setState(() => _deployingProjects.remove(info.projectId));
      }
    }
  }
}

class _DeploymentRow extends StatelessWidget {
  const _DeploymentRow({
    required this.info,
    required this.workflowLabel,
    required this.isDeploying,
    required this.onDeploy,
  });

  final DeploymentInfo info;
  final String workflowLabel;
  final bool isDeploying;
  final VoidCallback? onDeploy;

  Color _workflowColor(OpsPalette ops) {
    if (info.workflowStatus == 'in_progress') return ops.info;
    if (info.workflowConclusion == 'success') return ops.healthy;
    if (info.workflowConclusion == 'failure') return ops.danger;
    return ops.muted;
  }

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    final accent = info.repositoryAhead
        ? ops.gold
        : info.upToDate
        ? ops.healthy
        : ops.slate;
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(15),
      decoration: BoxDecoration(
        border: Border.all(color: ops.line),
        borderRadius: BorderRadius.circular(18),
        color: info.repositoryAhead ? ops.goldSoft : ops.surfaceAlt,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Icon(
                info.repositoryAhead
                    ? Icons.outbound_outlined
                    : Icons.check_circle_outline,
                color: accent,
              ),
              const SizedBox(width: 9),
              Expanded(
                child: Text(
                  info.projectName,
                  style: TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w900,
                    color: ops.ink,
                  ),
                ),
              ),
              // Workflow state as a single compact badge instead of a chip row.
              _WorkflowBadge(label: workflowLabel, color: _workflowColor(ops)),
              if (!info.deploymentEnabled) ...[
                const SizedBox(width: 8),
                Tooltip(
                  message: 'النشر اليدوي من التطبيق غير مفعل لهذا المشروع',
                  child: Icon(Icons.lock_outline, size: 19, color: ops.muted),
                ),
              ],
            ],
          ),
          const SizedBox(height: 12),
          // One compact line comparing server ↔ GitHub instead of three chips.
          _ReleaseLine(
            deployedSha: info.deployedShortSha,
            latestSha: info.latestShortSha,
            ahead: info.repositoryAhead,
          ),
          if (info.latestMessage.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              info.latestMessage,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: ops.slate, fontWeight: FontWeight.w700),
            ),
          ],
          const SizedBox(height: 9),
          Text(info.actionRequired, style: TextStyle(color: ops.slate)),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: Text(
                  '${info.repository} · ${info.branch}',
                  textDirection: TextDirection.ltr,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: ops.muted, fontSize: 12),
                ),
              ),
              ElevatedButton.icon(
                key: ValueKey('deploy-${info.projectId}'),
                onPressed: onDeploy,
                icon: isDeploying
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: Colors.white,
                        ),
                      )
                    : const Icon(Icons.rocket_launch_outlined),
                label: Text(isDeploying ? 'جاري التشغيل' : 'نشر الآن'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Compact "server ↔ GitHub" release comparison replacing the old chip stack.
class _ReleaseLine extends StatelessWidget {
  const _ReleaseLine({
    required this.deployedSha,
    required this.latestSha,
    required this.ahead,
  });
  final String deployedSha;
  final String latestSha;
  final bool ahead;

  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        border: Border.all(color: ops.lineSoft),
        borderRadius: BorderRadius.circular(12),
        color: ops.surface,
      ),
      child: Row(
        children: [
          _sha(ops, Icons.dns_outlined, 'الخادم', deployedSha),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10),
            child: Icon(
              ahead ? Icons.arrow_back_rounded : Icons.check_rounded,
              size: 16,
              color: ahead ? ops.gold : ops.healthy,
            ),
          ),
          _sha(ops, Icons.commit, 'GitHub', latestSha),
        ],
      ),
    );
  }

  Widget _sha(OpsPalette ops, IconData icon, String label, String sha) {
    return Expanded(
      child: Row(
        children: [
          Icon(icon, size: 16, color: ops.accentBlue),
          const SizedBox(width: 6),
          Text(
            '$label ',
            style: TextStyle(color: ops.muted, fontSize: 12),
          ),
          Flexible(
            child: Text(
              sha.isEmpty ? '—' : sha,
              textDirection: TextDirection.ltr,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontWeight: FontWeight.w900, color: ops.ink),
            ),
          ),
        ],
      ),
    );
  }
}

class _WorkflowBadge extends StatelessWidget {
  const _WorkflowBadge({required this.label, required this.color});
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: .12),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 7,
            height: 7,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 6),
          Text(
            label,
            style: TextStyle(
              color: color,
              fontSize: 11,
              fontWeight: FontWeight.w800,
            ),
          ),
        ],
      ),
    );
  }
}

class _SummaryItem extends StatelessWidget {
  const _SummaryItem({
    required this.label,
    required this.value,
    required this.icon,
    required this.color,
  });
  final String label;
  final String value;
  final IconData icon;
  final Color color;
  @override
  Widget build(BuildContext context) {
    final ops = context.ops;
    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
        child: Column(
          children: [
            Icon(icon, color: color),
            const SizedBox(height: 7),
            Text(
              value,
              style: TextStyle(
                fontSize: 23,
                fontWeight: FontWeight.w900,
                color: ops.ink,
              ),
            ),
            Text(label, style: TextStyle(color: ops.slate)),
          ],
        ),
      ),
    );
  }
}

class _ServerPanel extends StatelessWidget {
  const _ServerPanel({required this.server});
  final ServerInfo server;
  @override
  Widget build(BuildContext context) => Card(
    child: Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              const Icon(
                Icons.dns_outlined,
                color: Color(0xFF006C35),
                size: 28,
              ),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      server.name,
                      style: const TextStyle(
                        fontSize: 17,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    Text(
                      [
                        server.serverType,
                        server.publicIp,
                      ].where((value) => value?.isNotEmpty == true).join(' · '),
                      textDirection: TextDirection.ltr,
                      textAlign: TextAlign.right,
                      style: TextStyle(color: context.ops.slate),
                    ),
                  ],
                ),
              ),
              StatusDot(server.status),
            ],
          ),
        ),
        const Divider(height: 1),
        Padding(
          padding: const EdgeInsets.all(16),
          child: LayoutBuilder(
            builder: (context, box) {
              final bars = [
                UsageBar(
                  label: 'المعالج',
                  value: server.cpu,
                  icon: Icons.memory,
                ),
                UsageBar(
                  label: 'الذاكرة',
                  value: server.memory,
                  icon: Icons.storage_outlined,
                ),
                UsageBar(
                  label: 'القرص',
                  value: server.disk,
                  icon: Icons.disc_full_outlined,
                ),
              ];
              return box.maxWidth > 650
                  ? Row(
                      children: [
                        for (var i = 0; i < bars.length; i++) ...[
                          Expanded(child: bars[i]),
                          if (i < bars.length - 1) const SizedBox(width: 24),
                        ],
                      ],
                    )
                  : Column(
                      children: [
                        for (final bar in bars)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 14),
                            child: bar,
                          ),
                      ],
                    );
            },
          ),
        ),
        const Divider(height: 1),
        ...server.projects.map((project) => _ProjectRow(project: project)),
      ],
    ),
  );
}

class _ProjectRow extends StatelessWidget {
  const _ProjectRow({required this.project});
  final ProjectInfo project;
  @override
  Widget build(BuildContext context) => InkWell(
    onTap: () => Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) =>
            ProjectScreen(projectId: project.id, projectName: project.name),
      ),
    ),
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      child: Row(
        children: [
          StatusDot(project.status, showLabel: false),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  project.name,
                  style: TextStyle(
                    fontWeight: FontWeight.w800,
                    color: context.ops.ink,
                  ),
                ),
                Text(
                  project.baseUrl,
                  textDirection: TextDirection.ltr,
                  textAlign: TextAlign.right,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: context.ops.slate, fontSize: 12),
                ),
                if (project.latestMetric != null) ...[
                  const SizedBox(height: 5),
                  Text(
                    'CPU ${_usage(project.latestMetric!.cpu)} · '
                    'RAM ${_usage(project.latestMetric!.memory)} · '
                    '${project.latestMetric!.runningContainerCount}/'
                    '${project.latestMetric!.containerCount} حاوية تعمل',
                    textDirection: TextDirection.ltr,
                    textAlign: TextAlign.right,
                    style: TextStyle(
                      color: context.ops.emerald,
                      fontSize: 11,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ],
              ],
            ),
          ),
          if (project.latencyMs != null)
            Text(
              '${project.latencyMs} ms',
              textDirection: TextDirection.ltr,
              style: TextStyle(
                color: context.ops.slate,
                fontWeight: FontWeight.w700,
              ),
            ),
          const SizedBox(width: 8),
          Icon(Icons.chevron_left, color: context.ops.muted),
        ],
      ),
    ),
  );

  static String _usage(double? value) =>
      value == null ? '—' : '${value.toStringAsFixed(1)}%';
}

class _IncidentsPanel extends ConsumerWidget {
  const _IncidentsPanel({required this.data});
  final DashboardData data;
  @override
  Widget build(BuildContext context, WidgetRef ref) => Card(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Icon(
                Icons.notifications_active_outlined,
                color: context.ops.danger,
              ),
              const SizedBox(width: 9),
              Text(
                'التنبيهات المفتوحة',
                style: TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w900,
                  color: context.ops.ink,
                ),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        if (data.incidents.isEmpty)
          const EmptyState(
            icon: Icons.notifications_none,
            title: 'لا توجد تنبيهات',
            message: 'ستظهر هنا أي حالة تحتاج إلى تدخل.',
          )
        else
          ...data.incidents.map(
            (incident) => Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 4),
              child: Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: incident.severity == 'critical'
                      ? context.ops.dangerSoft
                      : context.ops.goldSoft,
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: context.ops.lineSoft),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      incident.title,
                      style: TextStyle(
                        fontWeight: FontWeight.w900,
                        color: context.ops.ink,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      incident.message,
                      style: TextStyle(color: context.ops.slate),
                    ),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            incident.projectName,
                            style: const TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        TextButton.icon(
                          onPressed: () async {
                            try {
                              await ref
                                  .read(dashboardProvider.notifier)
                                  .acknowledge(incident.id);
                            } on ApiException catch (error) {
                              if (context.mounted) {
                                ScaffoldMessenger.of(context).showSnackBar(
                                  SnackBar(content: Text(error.message)),
                                );
                              }
                            }
                          },
                          icon: const Icon(Icons.done, size: 18),
                          label: const Text('تم الاطلاع'),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        const SizedBox(height: 10),
      ],
    ),
  );
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.cloud_off_outlined,
            size: 54,
            color: context.ops.danger,
          ),
          const SizedBox(height: 14),
          Text(
            message,
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: 16,
              fontWeight: FontWeight.w700,
              color: context.ops.ink,
            ),
          ),
          const SizedBox(height: 18),
          ElevatedButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh),
            label: const Text('إعادة المحاولة'),
          ),
        ],
      ),
    ),
  );
}
