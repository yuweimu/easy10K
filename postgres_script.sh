echo $1
echo "select count(*) FROM report_digestion WHERE stock_code = '$1';"
echo "select count(*) FROM balance_sheet_items WHERE stock_code = '$1';"
echo "select count(*) FROM income_statement_items WHERE stock_code = '$1';"
echo "select count(*) FROM cash_flow_statement_items WHERE stock_code = '$1';"

echo

echo "DELETE FROM report_digestion WHERE stock_code = '$1';"
echo "DELETE FROM balance_sheet_items WHERE stock_code = '$1';"
echo "DELETE FROM income_statement_items WHERE stock_code = '$1';"
echo "DELETE FROM cash_flow_statement_items WHERE stock_code = '$1';"

