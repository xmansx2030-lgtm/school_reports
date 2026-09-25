class AppConfig {
  static const apiBaseUrl = String.fromEnvironment(
    'OPS_API_BASE_URL',
    defaultValue: 'https://tawtheeq-ksa.com/api/operations/v1',
  );

  static const provisionedAccessToken = String.fromEnvironment(
    'OPS_ACCESS_TOKEN',
  );

  /// Public Worker URL only. The emergency credential is entered on the device.
  static const emergencyUrl = String.fromEnvironment('OPS_EMERGENCY_URL');
}
