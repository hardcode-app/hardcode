{{- define "hardcode.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hardcode.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "hardcode.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "hardcode.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "hardcode.adminSelector" -}}
app.kubernetes.io/name: {{ include "hardcode.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: admin
{{- end -}}

{{- define "hardcode.bootstrapSecret" -}}
{{ .Values.admin.existingSecret | default (printf "%s-admin-bootstrap" (include "hardcode.fullname" .)) }}
{{- end -}}

{{- define "hardcode.smtpSecret" -}}
{{ .Values.admin.smtp.existingSecret | default (printf "%s-admin-smtp" (include "hardcode.fullname" .)) }}
{{- end -}}

{{- /* The portal's address for the admins' emails: admin.url, or the
     Ingress's host when there is one. */ -}}
{{- define "hardcode.adminURL" -}}
{{- if .Values.admin.url -}}
{{ .Values.admin.url }}
{{- else if .Values.admin.ingress.enabled -}}
{{ if .Values.admin.ingress.tls }}https{{ else }}http{{ end }}://{{ .Values.admin.ingress.host }}
{{- end -}}
{{- end -}}
