import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/api_client.dart';
import 'package:tawtheeq_operations/src/models.dart';

void main() {
  test('accepts only a non-empty access token', () {
    expect(parseOperationsToken({'token': '  secure-token  '}), 'secure-token');
    expect(
      () => parseOperationsToken({'token': null}),
      throwsA(isA<ApiException>()),
    );
    expect(
      () => parseOperationsToken({'token': ''}),
      throwsA(isA<ApiException>()),
    );
  });

  test('prefers a provisioned device token over stale local storage', () {
    expect(
      resolveOperationsToken(
        storedToken: 'old-token',
        provisionedToken: '  device-token  ',
      ),
      'device-token',
    );
    expect(
      resolveOperationsToken(storedToken: ' stored ', provisionedToken: ''),
      'stored',
    );
    expect(
      resolveOperationsToken(storedToken: null, provisionedToken: ''),
      isNull,
    );
  });

  test(
    'maps backend health states without treating unknown values as healthy',
    () {
      expect(healthStatusFrom('healthy'), HealthStatus.healthy);
      expect(healthStatusFrom('down'), HealthStatus.down);
      expect(healthStatusFrom('unexpected'), HealthStatus.unknown);
    },
  );

  test('parses a dashboard inventory payload', () {
    final dashboard = DashboardData.fromJson({
      'summary': {
        'servers': 1,
        'projects': 1,
        'healthy_projects': 1,
        'open_incidents': 0,
      },
      'servers': [
        {
          'id': 1,
          'name': 'main',
          'provider': 'hetzner',
          'status': 'healthy',
          'projects': [
            {
              'id': 7,
              'name': 'Project',
              'slug': 'project',
              'base_url': 'https://example.com',
              'status': 'healthy',
              'services': [],
            },
          ],
        },
      ],
      'incidents': [],
      'current_user': {
        'id': 3,
        'name': 'Ops',
        'phone': '0500000000',
        'role': 'operator',
        'role_label': 'مشغّل',
        'capabilities': ['view', 'run_checks'],
      },
      'agent': {'ready': false, 'label': 'غير مفعّل'},
      'generated_at': '2026-08-21T12:00:00Z',
    });

    expect(dashboard.serverCount, 1);
    expect(dashboard.servers.single.projects.single.id, 7);
    expect(dashboard.generatedAt, isNotNull);
    expect(dashboard.currentUser.can('run_checks'), isTrue);
    expect(dashboard.currentUser.can('manage_team'), isFalse);
    expect(dashboard.agentReady, isFalse);
  });

  test('parses a server-managed deployment release reference', () {
    final info = DeploymentInfo.fromJson({
      'project_id': 4,
      'project_slug': 'mizaan-beta',
      'project_name': 'Mizaan Beta',
      'deployed_image': 'mizaan-beta-web:authority-scope-20260905-00b754f',
      'deployed_reference': 'authority-scope-20260905-00b754f',
      'monitoring_mode': 'server',
    });

    expect(info.monitoringMode, 'server');
    expect(info.deployedReference, 'authority-scope-20260905-00b754f');
    expect(info.repository, isEmpty);
    expect(info.canDeploy, isFalse);
  });

  test(
    'keeps legacy superuser access and labels a missing agent as disabled',
    () {
      final dashboard = DashboardData.fromJson({
        'summary': const {},
        'servers': const [],
        'incidents': const [],
      });

      expect(dashboard.currentUser.isSuperuser, isTrue);
      expect(dashboard.currentUser.can('run_checks'), isTrue);
      expect(dashboard.currentUser.can('manage_team'), isTrue);
      expect(dashboard.agentReady, isFalse);
      expect(dashboard.agentLabel, 'غير مفعّل');
    },
  );
}
