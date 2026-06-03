import os
import csv
import math
from collections import Counter
from datetime import datetime, timezone
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

CSV_PATH = os.environ.get('CSV_PATH') or os.path.join(os.path.dirname(__file__), 'banco_limpo - Copia.csv')
REPORTS_DIR = os.path.join(os.path.dirname(__file__), 'reports')
USECOLS = ['_id', 'cpf', 'nome', 'sexo', 'nacionalidade', 'estado', 'cidade', 'bairro', 'dataNascimento', 'religiao']


def ensure_reports_dir():
    os.makedirs(REPORTS_DIR, exist_ok=True)


def parse_csv_date(value):
    """Tenta analisar uma data vinda do CSV em formatos comuns.

    Retorna um objeto datetime com tzinfo=UTC ou None se não for possível.
    """
    if not value:
        return None

    s = str(value).strip()

    # Tentar ISO com Z ou timezone
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        pass

    # Tentar formatos de data comuns
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            dt = datetime.strptime(s, fmt)
            return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        except Exception:
            continue

    # Tentar com hora
    for fmt in ('%d/%m/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def load_csv_preview(limit=300):
    """Carrega um preview leve do CSV usando apenas colunas úteis para analytics."""
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError('Arquivo banco_limpo - Copia.csv não encontrado.')

    with open(CSV_PATH, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return []

        indexes = {name: header.index(name) for name in USECOLS if name in header}
        rows = []

        row_count = 0
        for row in reader:
            if limit is not None and row_count >= limit:
                break

            rows.append({name: row[index] if index < len(row) else '' for name, index in indexes.items()})
            row_count += 1

    return rows


def load_csv_patients(limit=100):
    """Carrega pacientes do CSV limpo em formato compatível com o dashboard."""
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError('Arquivo banco_limpo - Copia.csv não encontrado.')

    with open(CSV_PATH, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return []

        indexes = {name: header.index(name) for name in ['_id', 'cpf', 'nome', 'sexo', 'estado', 'cidade', 'dataNascimento'] if name in header}
        rows = []

        for row_index, row in enumerate(reader, start=1):
            if limit is not None and len(rows) >= limit:
                break

            birth_value = row[indexes['dataNascimento']] if 'dataNascimento' in indexes and indexes['dataNascimento'] < len(row) else ''
            age = None
            try:
                birth = parse_csv_date(birth_value)
                if birth is not None:
                    age = (datetime.now(timezone.utc) - birth).days // 365
            except Exception:
                age = None

            # prefer CSV _id column if available
            csv_id = None
            if '_id' in indexes and indexes['_id'] < len(row):
                csv_id = row[indexes['_id']].strip() or None

            rows.append({
                'id': int(csv_id) if csv_id and csv_id.isdigit() else row_index,
                'full_name': row[indexes['nome']] if 'nome' in indexes and indexes['nome'] < len(row) else 'Paciente sem nome',
                'birth_date': birth_value or None,
                'age': age,
                'gender': row[indexes['sexo']] if 'sexo' in indexes and indexes['sexo'] < len(row) else 'Não informado',
                'phone': row[indexes['cpf']] if 'cpf' in indexes and indexes['cpf'] < len(row) else None,
                'document_number': row[indexes['cpf']] if 'cpf' in indexes and indexes['cpf'] < len(row) else None,
                'city': row[indexes['cidade']] if 'cidade' in indexes and indexes['cidade'] < len(row) else None,
                'created_at': None,
            })

    return rows


def find_csv_record_indexes(header):
    tipo_indexes = {}
    administrada_indexes = {}
    medication_description_indexes = {}

    for idx, col in enumerate(header):
        if col.startswith('registros[') and col.endswith('.tipo'):
            record_id = int(col[col.index('[') + 1:col.index(']')])
            tipo_indexes[record_id] = idx
        elif col.startswith('registros[') and col.endswith('.informacoes.dataAdministrada'):
            record_id = int(col[col.index('[') + 1:col.index(']')])
            administrada_indexes[record_id] = idx
        elif col.startswith('registros[') and col.endswith('.informacoes.medicamento.descricao'):
            record_id = int(col[col.index('[') + 1:col.index(']')])
            medication_description_indexes[record_id] = idx

    return tipo_indexes, administrada_indexes, medication_description_indexes


def extract_csv_records(tipo_indexes, administrada_indexes, medication_description_indexes, row):
    records = []
    for record_id in sorted(tipo_indexes.keys()):
        tipo_idx = tipo_indexes[record_id]
        if tipo_idx >= len(row):
            continue

        tipo = row[tipo_idx].strip()
        if not tipo:
            continue

        administered = ''
        if record_id in administrada_indexes:
            administrada_idx = administrada_indexes[record_id]
            if administrada_idx < len(row):
                administered = row[administrada_idx].strip()

        description = ''
        if record_id in medication_description_indexes:
            med_desc_idx = medication_description_indexes[record_id]
            if med_desc_idx < len(row):
                description = row[med_desc_idx].strip()

        status = 'concluído' if administered else 'pendente'
        category = 'other'
        if tipo in ('ATENDIMENTO', 'TRIAGEM', 'CIRURGIA'):
            category = 'appointment'
        elif tipo == 'MEDICAMENTO':
            category = 'exam'

        records.append({
            'type': tipo,
            'category': category,
            'description': description or None,
            'administered_at': administered or None,
            'status': status
        })

    return records


def load_csv_patient_details(patient_id, limit=None):
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError('Arquivo banco_limpo - Copia.csv não encontrado.')

    with open(CSV_PATH, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return None

        tipo_indexes, administrada_indexes, medication_description_indexes = find_csv_record_indexes(header)

        for row_index, row in enumerate(reader, start=1):
            if limit is not None and row_index > limit:
                break

            if row_index != patient_id:
                continue

            birth_value = row[header.index('dataNascimento')] if 'dataNascimento' in header else ''
            age = None
            try:
                birth = parse_csv_date(birth_value)
                if birth is not None:
                    age = (datetime.now(timezone.utc) - birth).days // 365
            except Exception:
                age = None

            patient = {
                'id': row_index,
                'full_name': row[header.index('nome')] if 'nome' in header and header.index('nome') < len(row) else 'Paciente sem nome',
                'birth_date': birth_value or None,
                'age': age,
                'gender': row[header.index('sexo')] if 'sexo' in header and header.index('sexo') < len(row) else 'Não informado',
                'phone': row[header.index('cpf')] if 'cpf' in header and header.index('cpf') < len(row) else None,
                'document_number': row[header.index('cpf')] if 'cpf' in header and header.index('cpf') < len(row) else None,
                'city': row[header.index('cidade')] if 'cidade' in header and header.index('cidade') < len(row) else None,
            }

            records = extract_csv_records(tipo_indexes, administrada_indexes, medication_description_indexes, row)
            appointments = [
                {
                    'record_type': rec['type'],
                    'status': rec['status'],
                    'description': rec['description'] or rec['type'],
                    'administered_at': rec['administered_at']
                }
                for rec in records if rec['category'] == 'appointment'
            ]
            exams = [
                {
                    'record_type': rec['type'],
                    'status': rec['status'],
                    'description': rec['description'] or 'Exame / Medicamento',
                    'administered_at': rec['administered_at']
                }
                for rec in records if rec['category'] == 'exam'
            ]

            return {
                'patient': patient,
                'appointments': appointments,
                'exams': exams
            }

    return None


def summarize_csv(limit=300):
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError('Arquivo banco_limpo - Copia.csv não encontrado.')

    with open(CSV_PATH, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return {
                'total_records': 0,
                'total_appointments': 0,
                'pending_exams': 0,
                'return_rate': 0,
                'gender_counts': {},
                'city_counts': {},
                'state_counts': {},
                'birth_month_counts': {},
                'age_groups': {}
            }

        indexes = {name: idx for idx, name in enumerate(header)}
        row_indexes = {
            'sexo': indexes.get('sexo'),
            'cidade': indexes.get('cidade'),
            'estado': indexes.get('estado'),
            'dataNascimento': indexes.get('dataNascimento')
        }

        tipo_indexes, administrada_indexes, _ = find_csv_record_indexes(header)
        tipo_order = sorted(tipo_indexes.items())

        total_records = 0
        gender_counts = Counter()
        city_counts = Counter()
        state_counts = Counter()
        birth_month_counts = Counter()
        age_groups = {'0-17': 0, '18-29': 0, '30-49': 0, '50+': 0}
        total_appointments = 0
        pending_exams = 0
        return_patients = 0

        for row in reader:
            if limit is not None and total_records >= limit:
                break

            total_records += 1
            gender = (row[row_indexes['sexo']].strip() if row_indexes['sexo'] is not None and row_indexes['sexo'] < len(row) else '') or 'Não informado'
            city = (row[row_indexes['cidade']].strip() if row_indexes['cidade'] is not None and row_indexes['cidade'] < len(row) else '') or 'Não informado'
            state = (row[row_indexes['estado']].strip() if row_indexes['estado'] is not None and row_indexes['estado'] < len(row) else '') or 'Não informado'
            birth_value = row[row_indexes['dataNascimento']].strip() if row_indexes['dataNascimento'] is not None and row_indexes['dataNascimento'] < len(row) else ''

            gender_counts[gender] += 1
            city_counts[city] += 1
            state_counts[state] += 1

            if birth_value:
                try:
                    birth = parse_csv_date(birth_value)
                    if birth is not None:
                        birth_month_counts[birth.strftime('%Y-%m')] += 1
                        age = (datetime.now(timezone.utc) - birth).days // 365
                        if age < 18:
                            age_groups['0-17'] += 1
                        elif age < 30:
                            age_groups['18-29'] += 1
                        elif age < 50:
                            age_groups['30-49'] += 1
                        else:
                            age_groups['50+'] += 1
                except Exception:
                    pass

            appointment_record_count = 0
            for record_id, tipo_idx in tipo_order:
                if tipo_idx >= len(row):
                    continue
                tipo_value = row[tipo_idx].strip()
                if not tipo_value:
                    continue
                if tipo_value in ('ATENDIMENTO', 'TRIAGEM', 'CIRURGIA'):
                    total_appointments += 1
                    appointment_record_count += 1
                elif tipo_value == 'MEDICAMENTO':
                    administrada_idx = administrada_indexes.get(record_id)
                    administrada_value = ''
                    if administrada_idx is not None and administrada_idx < len(row):
                        administrada_value = row[administrada_idx].strip()
                    if not administrada_value:
                        pending_exams += 1

            if appointment_record_count > 1:
                return_patients += 1

        return_rate = round((return_patients / total_records) * 100, 2) if total_records else 0

        return {
            'total_records': total_records,
            'total_appointments': total_appointments,
            'pending_exams': pending_exams,
            'return_rate': return_rate,
            'gender_counts': dict(gender_counts.most_common(6)),
            'city_counts': dict(city_counts.most_common(8)),
            'state_counts': dict(state_counts.most_common(8)),
            'birth_month_counts': dict(sorted(birth_month_counts.items())),
            'age_groups': age_groups,
        }


def chart_to_image(title, labels, values, output_path, kind='bar'):
    plt.figure(figsize=(6, 3.2), dpi=140)
    if kind == 'pie':
        plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=90)
    else:
        colors_list = plt.cm.Set2.colors[:len(labels)]
        plt.bar(labels, values, color=colors_list)
        plt.xticks(rotation=25, ha='right')
    plt.title(title, fontsize=9)
    plt.tight_layout()
    plt.savefig(output_path, bbox_inches='tight')
    plt.close('all')


def generate_pdf_report(output_name=None):
    ensure_reports_dir()
    if output_name is None:
        output_name = f'relatorio_csv_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'

    output_path = os.path.join(REPORTS_DIR, output_name)
    summary = summarize_csv(limit=300)

    gender_img = os.path.join(REPORTS_DIR, 'gender_chart.png')
    city_img = os.path.join(REPORTS_DIR, 'city_chart.png')

    chart_to_image('Sexo', list(summary['gender_counts'].keys()), list(summary['gender_counts'].values()), gender_img, kind='pie')
    chart_to_image('Cidades principais', list(summary['city_counts'].keys()), list(summary['city_counts'].values()), city_img, kind='bar')

    doc = SimpleDocTemplate(output_path, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph('Relatório Clínico - Fonte CSV', styles['Title']))
    story.append(Paragraph('Gerado automaticamente a partir de banco_limpo - Copia.csv', styles['Heading2']))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph(f'Total de registros analisados: {summary["total_records"]}', styles['Heading3']))

    summary_table = Table([
        ['Métrica', 'Valor'],
        ['Registros lidos', f"{summary['total_records']}"],
        ['Faixa etária 0-17', f"{summary['age_groups']['0-17']}"],
        ['Faixa etária 18-29', f"{summary['age_groups']['18-29']}"],
        ['Faixa etária 30-49', f"{summary['age_groups']['30-49']}"],
        ['Faixa etária 50+', f"{summary['age_groups']['50+']}"],
    ], colWidths=[6.5 * cm, 8.5 * cm])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f9d76')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.6 * cm))

    story.append(Paragraph('Distribuição por sexo', styles['Heading3']))
    story.append(Image(gender_img, width=8 * cm, height=5 * cm))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph('Principais cidades', styles['Heading3']))
    story.append(Image(city_img, width=8 * cm, height=5 * cm))

    doc.build(story)
    return output_path
