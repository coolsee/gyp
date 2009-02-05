{
  'conditions': [
    [ 'OS=="win"', {
      'target_defaults': {
        'defines': [
          '_CRT_SECURE_NO_DEPRECATE',
          '_CRT_NONSTDC_NO_WARNINGS',
          '_CRT_NONSTDC_NO_DEPRECATE',
          '_SCL_SECURE_NO_DEPRECATE',
        ],
        'msvs_disabled_warnings': [4800],
        'msvs_settings': {
          'VCCLCompilerTool': {
            'WarnAsError': 'false',
            'Detect64BitPortabilityProblems': 'false',
          },
        },
      },
    }],
    [ 'OS=="mac"', {
      'target_defaults': {
        'xcode_settings': {
          'GCC_TREAT_WARNINGS_AS_ERRORS': 'NO',
          'WARNING_CFLAGS!': ['-Wall'],
        },
      },
    }],
  ],
}
