; Kaitoi for DaVinci Resolve - Windows installer
; Built with Inno Setup (https://jrsoftware.org/isinfo.php)
;
;   ISCC /DMyAppVersion=0.1.0 packaging\windows\kaitoi-resolve.iss
;
; It installs, for every user of the PC:
; - the "Kaitoi" script, where Resolve lists it under Workspace > Scripts > Edit
; - the plugin's code, in Program Files: outside Resolve's Scripts folders
;   (Resolve would otherwise list each of its files in that menu) and where
;   only an administrator can change it (ProgramData is writable by every user)
; - the Kaitoi generator and effect, in Resolve's Effects Library
; and removes all of it on uninstall. The plugin is Python and pictures; it
; has no program of its own. Resolve needs Python 3 installed to run scripts.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Kaitoi for DaVinci Resolve"
#define MyAppPublisher "Kaitoi Labs, Inc."
#define MyAppURL "https://kaitoi.io"
#define Repo "..\.."
#define Fusion "{commonappdata}\Blackmagic Design\DaVinci Resolve\Fusion"

[Setup]
AppId={{6E0B1D52-3C1F-4B7A-9A53-2C7B6E5D8F41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Kaitoi\Resolve
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir={#Repo}\dist
OutputBaseFilename=Kaitoi-for-DaVinci-Resolve-{#MyAppVersion}-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#Repo}\LICENSE
InfoAfterFile=after-install.txt
#ifdef KAITOI_SIGN
; CI defines KAITOI_SIGN and the kaitoisign tool (ISCC /S) when the signing
; secrets are available. Inno then signs the uninstaller and the finished setup.
; Each signing is billed, so a failed signing fails the build instead of retrying.
SignTool=kaitoisign
SignedUninstaller=yes
SignToolRetryCount=0
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#Repo}\Kaitoi.py"; DestDir: "{#Fusion}\Scripts\Edit"; Flags: ignoreversion
Source: "{#Repo}\templates\Edit\Generators\Kaitoi.setting"; DestDir: "{#Fusion}\Templates\Edit\Generators"; Flags: ignoreversion
Source: "{#Repo}\templates\Edit\Effects\Kaitoi.setting"; DestDir: "{#Fusion}\Templates\Edit\Effects"; Flags: ignoreversion
Source: "{#Repo}\kaitoi_resolve\*"; DestDir: "{app}\kaitoi_resolve"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Repo}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; A copy left in the installing user's own Scripts folder by an earlier manual
; install would be listed in Resolve beside this one.
Type: files; Name: "{userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\Kaitoi.py"
Type: files; Name: "{userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\Kaitoi Video.py"
Type: filesandordirs; Name: "{userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\kaitoi_resolve"
; Python may have left caches beside an older version of the code.
Type: filesandordirs; Name: "{app}\kaitoi_resolve"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\kaitoi_resolve"
