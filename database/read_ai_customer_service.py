from pathlib import Path

from openpyxl import load_workbook


# Excel 文件和本 Python 文件放在同一个文件夹中
excel_file = Path(__file__).parent / "AI客服优化.xlsx"


def main() -> None:
    if not excel_file.exists():
        print(f"找不到文件：{excel_file}")
        return

    # data_only=True：读取单元格中保存的计算结果，而不是公式本身
    workbook = load_workbook(excel_file, data_only=True, read_only=True)

    print(f"文件：{excel_file.name}")
    print(f"工作表：{workbook.sheetnames}")

    for worksheet in workbook.worksheets:
        print(f"\n===== 工作表：{worksheet.title} =====")

        for row in worksheet.iter_rows(values_only=True):
            # 跳过完全为空的行
            if any(value is not None for value in row):
                print(row)

    workbook.close()


if __name__ == "__main__":
    main()
