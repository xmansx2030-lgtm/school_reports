enum HealthStatus { healthy, degraded, down, maintenance, unknown }

HealthStatus healthStatusFrom(String? value) => switch (value) {
  'healthy' => HealthStatus.healthy,
  'degraded' => HealthStatus.degraded,
  'down' => HealthStatus.down,
  'maintenance' => HealthStatus.maintenance,
  _ => HealthStatus.unknown,
};

double? _double(dynamic value) =>
    value == null ? null : double.tryParse('$value');
DateTime? _date(dynamic value) =>
    value == null ? null : DateTime.tryParse('$value')?.toLocal();

class ServiceInfo {
  const ServiceInfo({
    required this.id,
    required this.name,
    required this.key,
    required this.kindLabel,
    required this.status,
    required this.restartAllowed,
    this.lastCheckedAt,
  });
  final int id;
  final String name;
  final String key;
  final String kindLabel;
  final HealthStatus status;
  final bool restartAllowed;
  final DateTime? lastCheckedAt;

  factory ServiceInfo.fromJson(Map<String, dynamic> json) => ServiceInfo(
    id: json['id'] as int,
    name: '${json['name'] ?? ''}',
    key: '${json['service_key'] ?? ''}',
    kindLabel: '${json['kind_label'] ?? ''}',
    status: healthStatusFrom('${json['status'] ?? ''}'),
    restartAllowed: json['restart_allowed'] == true,
    lastCheckedAt: _date(json['last_checked_at']),
  );
}

class ProjectInfo {
  const ProjectInfo({
    required this.id,
    required this.name,
    required this.slug,
    required this.baseUrl,
    required this.status,
    required this.failures,
    required this.alertsEnabled,
    required this.services,
    this.latencyMs,
    this.lastCheckedAt,
    this.lastRuntimeCheckedAt,
    this.latestMetric,
  });
  final int id;
  final String name;
  final String slug;
  final String baseUrl;
  final HealthStatus status;
  final int? latencyMs;
  final int failures;
  final bool alertsEnabled;
  final DateTime? lastCheckedAt;
  final DateTime? lastRuntimeCheckedAt;
  final MetricPoint? latestMetric;
  final List<ServiceInfo> services;

  factory ProjectInfo.fromJson(Map<String, dynamic> json) => ProjectInfo(
    id: json['id'] as int,
    name: '${json['name'] ?? ''}',
    slug: '${json['slug'] ?? ''}',
    baseUrl: '${json['base_url'] ?? ''}',
    status: healthStatusFrom('${json['status'] ?? ''}'),
    latencyMs: (json['last_latency_ms'] as num?)?.toInt(),
    failures: (json['consecutive_failures'] as num?)?.toInt() ?? 0,
    alertsEnabled: json['alerts_enabled'] == true,
    lastCheckedAt: _date(json['last_checked_at']),
    lastRuntimeCheckedAt: _date(json['last_runtime_checked_at']),
    latestMetric: json['latest_metric'] is Map
        ? MetricPoint.fromJson(
            Map<String, dynamic>.from(json['latest_metric'] as Map),
          )
        : null,
    services: (json['services'] as List? ?? const [])
        .map(
          (item) =>
              ServiceInfo.fromJson(Map<String, dynamic>.from(item as Map)),
        )
        .toList(),
  );
}

class ServerInfo {
  const ServerInfo({
    required this.id,
    required this.name,
    required this.slug,
    required this.provider,
    required this.status,
    required this.projects,
    this.publicIp,
    this.serverType,
    this.cpu,
    this.memory,
    this.disk,
    this.lastCheckedAt,
  });
  final int id;
  final String name;
  final String slug;
  final String provider;
  final String? publicIp;
  final String? serverType;
  final HealthStatus status;
  final double? cpu;
  final double? memory;
  final double? disk;
  final DateTime? lastCheckedAt;
  final List<ProjectInfo> projects;

  factory ServerInfo.fromJson(Map<String, dynamic> json) => ServerInfo(
    id: json['id'] as int,
    name: '${json['name'] ?? ''}',
    slug: '${json['slug'] ?? ''}',
    provider: '${json['provider'] ?? ''}',
    publicIp: json['public_ip']?.toString(),
    serverType: json['server_type']?.toString(),
    status: healthStatusFrom('${json['status'] ?? ''}'),
    cpu: _double(json['cpu_percent']),
    memory: _double(json['memory_percent']),
    disk: _double(json['disk_percent']),
    lastCheckedAt: _date(json['last_checked_at']),
    projects: (json['projects'] as List? ?? const [])
        .map(
          (item) =>
              ProjectInfo.fromJson(Map<String, dynamic>.from(item as Map)),
        )
        .toList(),
  );
}

class ProviderOverview {
  const ProviderOverview({
    required this.configured,
    required this.canControl,
    required this.detail,
    this.server,
  });
  final bool configured;
  final bool canControl;
  final String detail;
  final ProviderServerInfo? server;

  factory ProviderOverview.fromJson(Map<String, dynamic> json) =>
      ProviderOverview(
        configured: json['configured'] == true,
        canControl: json['can_control'] == true,
        detail: '${json['detail'] ?? ''}',
        server: json['server'] is Map
            ? ProviderServerInfo.fromJson(
                Map<String, dynamic>.from(json['server'] as Map),
              )
            : null,
      );
}

class ProviderServerInfo {
  const ProviderServerInfo({
    required this.name,
    required this.status,
    required this.serverType,
    required this.location,
    required this.backupWindow,
    required this.deleteProtected,
    required this.backups,
    required this.recentActions,
    required this.metrics,
    required this.partialErrors,
    this.fetchedAt,
  });
  final String name;
  final String status;
  final String serverType;
  final String location;
  final String backupWindow;
  final bool deleteProtected;
  final List<Map<String, dynamic>> backups;
  final List<Map<String, dynamic>> recentActions;
  final Map<String, dynamic> metrics;
  final List<String> partialErrors;
  final DateTime? fetchedAt;

  factory ProviderServerInfo.fromJson(Map<String, dynamic> json) {
    final protection = Map<String, dynamic>.from(
      json['protection'] as Map? ?? const {},
    );
    return ProviderServerInfo(
      name: '${json['name'] ?? ''}',
      status: '${json['status'] ?? 'unknown'}',
      serverType: '${json['server_type'] ?? ''}',
      location: '${json['location'] ?? ''}',
      backupWindow: '${json['backup_window'] ?? ''}',
      deleteProtected: protection['delete'] == true,
      backups: (json['backups'] as List? ?? const [])
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList(),
      recentActions: (json['recent_actions'] as List? ?? const [])
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList(),
      metrics: Map<String, dynamic>.from(json['metrics'] as Map? ?? const {}),
      partialErrors: (json['partial_errors'] as List? ?? const [])
          .map((item) => '$item')
          .toList(),
      fetchedAt: _date(json['fetched_at']),
    );
  }

  double? latestMetric(String key) {
    final series = Map<String, dynamic>.from(
      metrics['time_series'] as Map? ?? const {},
    );
    final points = (series[key] as Map?)?['values'] as List?;
    if (points == null || points.isEmpty) return null;
    final last = points.last;
    if (last is! List || last.length < 2) return null;
    return _double(last[1]);
  }
}

class ProviderActionInfo {
  const ProviderActionInfo({
    required this.id,
    required this.action,
    required this.status,
    required this.errorCode,
  });
  final int id;
  final String action;
  final String status;
  final String errorCode;
  bool get finished => status == 'success' || status == 'error';
  factory ProviderActionInfo.fromJson(Map<String, dynamic> json) =>
      ProviderActionInfo(
        id: (json['id'] as num?)?.toInt() ?? 0,
        action: '${json['action'] ?? ''}',
        status: '${json['status'] ?? ''}',
        errorCode: '${json['error_code'] ?? ''}',
      );
}

class IncidentInfo {
  const IncidentInfo({
    required this.id,
    required this.title,
    required this.message,
    required this.severity,
    required this.status,
    required this.projectName,
    required this.openedAt,
  });
  final int id;
  final String title;
  final String message;
  final String severity;
  final String status;
  final String projectName;
  final DateTime? openedAt;

  factory IncidentInfo.fromJson(Map<String, dynamic> json) => IncidentInfo(
    id: json['id'] as int,
    title: '${json['title'] ?? ''}',
    message: '${json['message'] ?? ''}',
    severity: '${json['severity'] ?? 'warning'}',
    status: '${json['status'] ?? 'open'}',
    projectName: '${json['project_name'] ?? ''}',
    openedAt: _date(json['opened_at']),
  );
}

class DashboardData {
  const DashboardData({
    required this.servers,
    required this.incidents,
    required this.serverCount,
    required this.projectCount,
    required this.healthyProjectCount,
    required this.openIncidentCount,
    required this.teamMemberCount,
    required this.generatedAt,
    required this.currentUser,
    required this.agentReady,
    required this.agentLabel,
  });
  final List<ServerInfo> servers;
  final List<IncidentInfo> incidents;
  final int serverCount;
  final int projectCount;
  final int healthyProjectCount;
  final int openIncidentCount;
  final int teamMemberCount;
  final DateTime? generatedAt;
  final OperationsAccount currentUser;
  final bool agentReady;
  final String agentLabel;

  factory DashboardData.fromJson(Map<String, dynamic> json) {
    final summary = Map<String, dynamic>.from(
      json['summary'] as Map? ?? const {},
    );
    final agent = Map<String, dynamic>.from(json['agent'] as Map? ?? const {});
    final currentUserPayload = json['current_user'] as Map?;
    return DashboardData(
      servers: (json['servers'] as List? ?? const [])
          .map(
            (item) =>
                ServerInfo.fromJson(Map<String, dynamic>.from(item as Map)),
          )
          .toList(),
      incidents: (json['incidents'] as List? ?? const [])
          .map(
            (item) =>
                IncidentInfo.fromJson(Map<String, dynamic>.from(item as Map)),
          )
          .toList(),
      serverCount: (summary['servers'] as num?)?.toInt() ?? 0,
      projectCount: (summary['projects'] as num?)?.toInt() ?? 0,
      healthyProjectCount: (summary['healthy_projects'] as num?)?.toInt() ?? 0,
      openIncidentCount: (summary['open_incidents'] as num?)?.toInt() ?? 0,
      teamMemberCount: (summary['team_members'] as num?)?.toInt() ?? 0,
      generatedAt: _date(json['generated_at']),
      currentUser: currentUserPayload == null
          ? OperationsAccount.legacyOwner()
          : OperationsAccount.fromJson(
              Map<String, dynamic>.from(currentUserPayload),
            ),
      agentReady: agent['ready'] == true,
      agentLabel: '${agent['label'] ?? 'غير مفعّل'}',
    );
  }
}

class DeploymentInfo {
  const DeploymentInfo({
    required this.projectId,
    required this.projectSlug,
    required this.projectName,
    required this.repository,
    required this.branch,
    required this.workflow,
    required this.configured,
    required this.deploymentEnabled,
    required this.latestSha,
    required this.latestShortSha,
    required this.latestMessage,
    required this.deployedSha,
    required this.deployedShortSha,
    required this.deployedImage,
    required this.deployedReference,
    required this.monitoringMode,
    required this.upToDate,
    required this.repositoryAhead,
    required this.workflowStatus,
    required this.workflowConclusion,
    required this.workflowUrl,
    required this.actionRequired,
    required this.canDeploy,
    this.workflowRunId,
  });

  final int projectId;
  final String projectSlug;
  final String projectName;
  final String repository;
  final String branch;
  final String workflow;
  final bool configured;
  final bool deploymentEnabled;
  final String latestSha;
  final String latestShortSha;
  final String latestMessage;
  final String deployedSha;
  final String deployedShortSha;
  final String deployedImage;
  final String deployedReference;
  final String monitoringMode;
  final bool upToDate;
  final bool repositoryAhead;
  final String workflowStatus;
  final String workflowConclusion;
  final String workflowUrl;
  final String actionRequired;
  final bool canDeploy;
  final int? workflowRunId;

  factory DeploymentInfo.fromJson(Map<String, dynamic> json) => DeploymentInfo(
    projectId: (json['project_id'] as num?)?.toInt() ?? 0,
    projectSlug: '${json['project_slug'] ?? ''}',
    projectName: '${json['project_name'] ?? ''}',
    repository: '${json['repository'] ?? ''}',
    branch: '${json['branch'] ?? ''}',
    workflow: '${json['workflow'] ?? ''}',
    configured: json['configured'] == true,
    deploymentEnabled: json['deployment_enabled'] == true,
    latestSha: '${json['latest_sha'] ?? ''}',
    latestShortSha: '${json['latest_short_sha'] ?? ''}',
    latestMessage: '${json['latest_message'] ?? ''}',
    deployedSha: '${json['deployed_sha'] ?? ''}',
    deployedShortSha: '${json['deployed_short_sha'] ?? ''}',
    deployedImage: '${json['deployed_image'] ?? ''}',
    deployedReference:
        '${json['deployed_reference'] ?? json['deployed_short_sha'] ?? ''}',
    monitoringMode: '${json['monitoring_mode'] ?? 'repository'}',
    upToDate: json['up_to_date'] == true,
    repositoryAhead: json['repository_ahead'] == true,
    workflowStatus: '${json['workflow_status'] ?? ''}',
    workflowConclusion: '${json['workflow_conclusion'] ?? ''}',
    workflowUrl: '${json['workflow_url'] ?? ''}',
    actionRequired: '${json['action_required'] ?? ''}',
    canDeploy: json['can_deploy'] == true,
    workflowRunId: (json['workflow_run_id'] as num?)?.toInt(),
  );
}

class DeploymentOverview {
  const DeploymentOverview({
    required this.deployments,
    required this.repositoryAheadCount,
    required this.canDeployCount,
  });

  final List<DeploymentInfo> deployments;
  final int repositoryAheadCount;
  final int canDeployCount;

  factory DeploymentOverview.fromJson(Map<String, dynamic> json) =>
      DeploymentOverview(
        deployments: (json['deployments'] as List? ?? const [])
            .map(
              (item) => DeploymentInfo.fromJson(
                Map<String, dynamic>.from(item as Map),
              ),
            )
            .toList(),
        repositoryAheadCount:
            (json['repository_ahead_count'] as num?)?.toInt() ?? 0,
        canDeployCount: (json['can_deploy_count'] as num?)?.toInt() ?? 0,
      );
}

class MetricPoint {
  const MetricPoint({
    this.cpu,
    this.memory,
    this.memoryUsedMb,
    this.memoryLimitMb,
    this.networkRxMb,
    this.networkTxMb,
    this.blockReadMb,
    this.blockWriteMb,
    this.containerCount = 0,
    this.runningContainerCount = 0,
    this.capturedAt,
  });
  final double? cpu;
  final double? memory;
  final double? memoryUsedMb;
  final double? memoryLimitMb;
  final double? networkRxMb;
  final double? networkTxMb;
  final double? blockReadMb;
  final double? blockWriteMb;
  final int containerCount;
  final int runningContainerCount;
  final DateTime? capturedAt;
  factory MetricPoint.fromJson(Map<String, dynamic> json) => MetricPoint(
    cpu: _double(json['cpu_percent']),
    memory: _double(json['memory_percent']),
    memoryUsedMb: _double(json['memory_used_mb']),
    memoryLimitMb: _double(json['memory_limit_mb']),
    networkRxMb: _double(json['network_rx_mb']),
    networkTxMb: _double(json['network_tx_mb']),
    blockReadMb: _double(json['block_read_mb']),
    blockWriteMb: _double(json['block_write_mb']),
    containerCount: (json['container_count'] as num?)?.toInt() ?? 0,
    runningContainerCount:
        (json['running_container_count'] as num?)?.toInt() ?? 0,
    capturedAt: _date(json['captured_at']),
  );
}

class CheckPoint {
  const CheckPoint({
    required this.ok,
    this.statusCode,
    this.latencyMs,
    this.errorCode = '',
    this.checkedAt,
  });
  final bool ok;
  final int? statusCode;
  final int? latencyMs;
  final String errorCode;
  final DateTime? checkedAt;
  factory CheckPoint.fromJson(Map<String, dynamic> json) => CheckPoint(
    ok: json['ok'] == true,
    statusCode: (json['status_code'] as num?)?.toInt(),
    latencyMs: (json['latency_ms'] as num?)?.toInt(),
    errorCode: '${json['error_code'] ?? ''}',
    checkedAt: _date(json['checked_at']),
  );
}

class ProjectDetails {
  const ProjectDetails({
    required this.project,
    required this.metrics,
    required this.checks,
    required this.actions,
  });
  final ProjectInfo project;
  final List<MetricPoint> metrics;
  final List<CheckPoint> checks;
  final List<OperationActionInfo> actions;
  factory ProjectDetails.fromJson(Map<String, dynamic> json) => ProjectDetails(
    project: ProjectInfo.fromJson(json),
    metrics: (json['metrics'] as List? ?? const [])
        .map(
          (item) =>
              MetricPoint.fromJson(Map<String, dynamic>.from(item as Map)),
        )
        .toList(),
    checks: (json['checks'] as List? ?? const [])
        .map(
          (item) => CheckPoint.fromJson(Map<String, dynamic>.from(item as Map)),
        )
        .toList(),
    actions: (json['actions'] as List? ?? const [])
        .map(
          (item) => OperationActionInfo.fromJson(
            Map<String, dynamic>.from(item as Map),
          ),
        )
        .toList(),
  );
}

class OperationActionInfo {
  const OperationActionInfo({
    required this.id,
    required this.requestId,
    required this.action,
    required this.actionLabel,
    required this.status,
    required this.resultSummary,
    required this.errorCode,
    this.requestedAt,
    this.finishedAt,
    this.logContent = '',
    this.logAnalysis = const {},
  });
  final int id;
  final String requestId;
  final String action;
  final String actionLabel;
  final String status;
  final String resultSummary;
  final String errorCode;
  final DateTime? requestedAt;
  final DateTime? finishedAt;
  final String logContent;
  final Map<String, dynamic> logAnalysis;

  factory OperationActionInfo.fromJson(Map<String, dynamic> json) =>
      OperationActionInfo(
        id: json['id'] as int,
        requestId: '${json['request_id'] ?? ''}',
        action: '${json['action'] ?? ''}',
        actionLabel: '${json['action_label'] ?? ''}',
        status: '${json['status'] ?? ''}',
        resultSummary: '${json['result_summary'] ?? ''}',
        errorCode: '${json['error_code'] ?? ''}',
        requestedAt: _date(json['requested_at']),
        finishedAt: _date(json['finished_at']),
        logContent: '${json['log_content'] ?? ''}',
        logAnalysis: Map<String, dynamic>.from(
          json['log_analysis'] as Map? ?? const {},
        ),
      );
}

class OperationsAccount {
  const OperationsAccount({
    required this.id,
    required this.name,
    required this.phone,
    required this.email,
    required this.isActive,
    required this.isStaff,
    required this.isSuperuser,
    required this.role,
    required this.roleLabel,
    required this.capabilities,
    required this.activeDevices,
    this.dateJoined,
    this.lastSeenAt,
  });
  final int id;
  final String name;
  final String phone;
  final String email;
  final bool isActive;
  final bool isStaff;
  final bool isSuperuser;
  final String role;
  final String roleLabel;
  final Set<String> capabilities;
  final int activeDevices;
  final DateTime? dateJoined;
  final DateTime? lastSeenAt;

  bool can(String capability) => capabilities.contains(capability);

  factory OperationsAccount.legacyOwner() => const OperationsAccount(
    id: 0,
    name: '',
    phone: '',
    email: '',
    isActive: true,
    isStaff: true,
    isSuperuser: true,
    role: 'owner',
    roleLabel: 'مالك مركز العمليات',
    capabilities: {
      'view',
      'run_checks',
      'run_actions',
      'acknowledge_incidents',
      'manage_team',
      'view_payment_links',
      'manage_payment_links',
    },
    activeDevices: 0,
  );

  factory OperationsAccount.fromJson(Map<String, dynamic> json) =>
      OperationsAccount(
        id: (json['id'] as num?)?.toInt() ?? 0,
        name: '${json['name'] ?? ''}',
        phone: '${json['phone'] ?? ''}',
        email: '${json['email'] ?? ''}',
        isActive: json['is_active'] == true,
        isStaff: json['is_staff'] == true,
        isSuperuser: json['is_superuser'] == true,
        role: '${json['role'] ?? ''}',
        roleLabel: '${json['role_label'] ?? 'غير محدد'}',
        capabilities: (json['capabilities'] as List? ?? const [])
            .map((item) => '$item')
            .toSet(),
        activeDevices: (json['active_devices'] as num?)?.toInt() ?? 0,
        dateJoined: _date(json['date_joined']),
        lastSeenAt: _date(json['last_seen_at']),
      );
}

class PaymentProjectOption {
  const PaymentProjectOption({
    required this.id,
    required this.name,
    required this.slug,
  });

  final int id;
  final String name;
  final String slug;

  factory PaymentProjectOption.fromJson(Map<String, dynamic> json) =>
      PaymentProjectOption(
        id: (json['id'] as num?)?.toInt() ?? 0,
        name: '${json['name'] ?? ''}',
        slug: '${json['slug'] ?? ''}',
      );
}

class PaymentLinkInfo {
  const PaymentLinkInfo({
    required this.publicId,
    required this.projectId,
    required this.projectName,
    required this.customerName,
    required this.customerPhone,
    required this.customerEmail,
    required this.amount,
    required this.currency,
    required this.description,
    required this.internalReference,
    required this.gatewayUrl,
    required this.status,
    required this.statusLabel,
    required this.createdByName,
    required this.canCancel,
    required this.cancellationConfirmation,
    this.expiresAt,
    this.paidAt,
    this.lastSyncedAt,
    this.createdAt,
  });

  final String publicId;
  final int projectId;
  final String projectName;
  final String customerName;
  final String customerPhone;
  final String customerEmail;
  final double amount;
  final String currency;
  final String description;
  final String internalReference;
  final String gatewayUrl;
  final String status;
  final String statusLabel;
  final String createdByName;
  final bool canCancel;
  final String cancellationConfirmation;
  final DateTime? expiresAt;
  final DateTime? paidAt;
  final DateTime? lastSyncedAt;
  final DateTime? createdAt;

  bool get isPayable => status == 'initiated' || status == 'on_hold';

  factory PaymentLinkInfo.fromJson(Map<String, dynamic> json) =>
      PaymentLinkInfo(
        publicId: '${json['public_id'] ?? ''}',
        projectId: (json['project_id'] as num?)?.toInt() ?? 0,
        projectName: '${json['project_name'] ?? ''}',
        customerName: '${json['customer_name'] ?? ''}',
        customerPhone: '${json['customer_phone'] ?? ''}',
        customerEmail: '${json['customer_email'] ?? ''}',
        amount: _double(json['amount']) ?? 0,
        currency: '${json['currency'] ?? 'SAR'}',
        description: '${json['description'] ?? ''}',
        internalReference: '${json['internal_reference'] ?? ''}',
        gatewayUrl: '${json['gateway_url'] ?? ''}',
        status: '${json['status'] ?? 'provisioning'}',
        statusLabel: '${json['status_label'] ?? ''}',
        createdByName: '${json['created_by_name'] ?? ''}',
        canCancel: json['can_cancel'] == true,
        cancellationConfirmation: '${json['cancellation_confirmation'] ?? ''}',
        expiresAt: _date(json['expires_at']),
        paidAt: _date(json['paid_at']),
        lastSyncedAt: _date(json['last_synced_at']),
        createdAt: _date(json['created_at']),
      );
}

class PaymentLinksData {
  const PaymentLinksData({
    required this.links,
    required this.projects,
    required this.gatewayEnabled,
    required this.canManage,
  });

  final List<PaymentLinkInfo> links;
  final List<PaymentProjectOption> projects;
  final bool gatewayEnabled;
  final bool canManage;

  factory PaymentLinksData.fromJson(
    Map<String, dynamic> json,
  ) => PaymentLinksData(
    links: (json['payment_links'] as List? ?? const [])
        .map(
          (item) =>
              PaymentLinkInfo.fromJson(Map<String, dynamic>.from(item as Map)),
        )
        .toList(),
    projects: (json['projects'] as List? ?? const [])
        .map(
          (item) => PaymentProjectOption.fromJson(
            Map<String, dynamic>.from(item as Map),
          ),
        )
        .toList(),
    gatewayEnabled: json['gateway_enabled'] == true,
    canManage: json['can_manage'] == true,
  );
}

String normalizeWhatsAppPhone(String value) {
  var digits = value.replaceAll(RegExp(r'\D'), '');
  if (digits.startsWith('00')) digits = digits.substring(2);
  if (digits.length == 10 && digits.startsWith('05')) {
    digits = '966${digits.substring(1)}';
  } else if (digits.length == 9 && digits.startsWith('5')) {
    digits = '966$digits';
  }
  return digits;
}

Uri paymentLinkWhatsAppUri(PaymentLinkInfo link) {
  final expiry = link.expiresAt == null
      ? ''
      : '\nصلاحية الرابط حتى: ${link.expiresAt!.toIso8601String().split('T').first}';
  final reference = link.internalReference.isEmpty
      ? ''
      : '\nالمرجع: ${link.internalReference}';
  final message =
      'السلام عليكم ${link.customerName}،\n'
      'هذا رابط دفع خاص بخدمات ${link.projectName}.\n'
      '${link.description}\n'
      'المبلغ: ${link.amount.toStringAsFixed(2)} ${link.currency}'
      '$reference$expiry\n'
      '${link.gatewayUrl}\n'
      'يرجى عدم مشاركة رابط الدفع مع الآخرين.';
  return Uri.https('wa.me', '/${normalizeWhatsAppPhone(link.customerPhone)}', {
    'text': message,
  });
}
