import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO

# Page config
st.set_page_config(page_title="Walmart SSO Allocation Tool", layout="wide")

st.title("📦 Walmart SSO Allocation Tool")
st.markdown("Upload both input files in the sidebar and click **Process Data** to generate SSO quantities.")

# Sidebar
with st.sidebar:
    st.header("🗂 Upload Files")
    luminate_file = st.file_uploader("Raw Luminate Data (.csv)", type="csv")
    variants_file = st.file_uploader("Variants Data (.xlsx)", type="xlsx")
    process = st.button("🚀 Process Data")

if luminate_file and variants_file and process:
    try:
        # Load files
        df_raw_luminate = pd.read_csv(luminate_file)
        df_variants = pd.read_excel(variants_file)

        # Rename columns
        rename_columns = {
            "walmart_item_number": "Item_Nbr",
            "item_name": "Item_Description",
            "vendor_pack_quantity": "VNPK_Qty",
            "warehouse_pack_quantity": "WHPK_Qty",
            "store_number": "Store_Nbr",
            "Yesterday_store_on_hand_quantity_this_year_eop": "Curr_Str_On_Hand_Qty",
            "Yesterday_store_in_transit_quantity_this_year_eop": "Curr_Str_In_Transit_Qty",
            "Yesterday_store_in_warehouse_quantity_this_year_eop": "Curr_Str_In_Whse_Qty",
            "Yesterday_store_on_order_quantity_this_year_eop": "Curr_Str_On_Order_Qty",
            "L4W_pos_quantity_this_year": "POS_Qty",
            "distribution_center_number": "Assembly_Warehouse"
        }
        df_raw_luminate.rename(columns=rename_columns, inplace=True)
        df_luminate = df_raw_luminate[df_raw_luminate["Yesterday_valid_store_count_this_year"] != 0].copy()

        columns_to_drop = [
            "store_name", "Yesterday_valid_store_count_this_year", "Yesterday_traited_store_count_this_year",
            "Yesterday_pos_quantity_this_year", "Yesterday_repl_instock_percentage_this_year_eop",
            "L4W_store_on_hand_quantity_this_year_eop", "L4W_store_in_transit_quantity_this_year_eop",
            "L4W_store_in_warehouse_quantity_this_year_eop", "L4W_store_on_order_quantity_this_year_eop",
            "L4W_valid_store_count_this_year", "L4W_traited_store_count_this_year"
        ]
        df_luminate.drop(columns=columns_to_drop, inplace=True, errors='ignore')

        df_luminate["Average_POS"] = df_luminate["POS_Qty"] / 4
        df_luminate["Total_Pipeline_WM"] = (
            df_luminate["Curr_Str_On_Hand_Qty"] +
            df_luminate["Curr_Str_In_Transit_Qty"] +
            df_luminate["Curr_Str_In_Whse_Qty"] +
            df_luminate["Curr_Str_On_Order_Qty"]
        )
        df_luminate["WOS_WM"] = df_luminate["Total_Pipeline_WM"] / df_luminate["Average_POS"]

        df_merged = pd.merge(df_luminate, df_variants, on="Item_Nbr", how="left")
        df = df_merged.copy()
        df['SSO_Qty'] = 0

        def round_up_to_multiple(x, base):
            return int(base * np.ceil(x / base)) if x > 0 else 0

        def round_down_to_multiple(x, base):
            return int(base * np.floor(x / base)) if x > 0 else 0

        def calculate_required(row):
            return max(0, (row['WOS_Gerber'] * row['Average_POS']) - (
                row['Curr_Str_On_Hand_Qty'] +
                row['Curr_Str_In_Transit_Qty'] +
                row['Curr_Str_On_Order_Qty'] +
                row['Curr_Str_In_Whse_Qty']
            ))

        df['Required_Qty'] = df.apply(calculate_required, axis=1)
        df['Rounded_Required_Qty'] = df.apply(
            lambda row: round_up_to_multiple(row['Required_Qty'], row['WHPK_Qty']), axis=1
        )
        df['Final_Required_Qty'] = df[['Rounded_Required_Qty', 'Units_Cap']].min(axis=1)
        df['MR_Average_POS'] = df.groupby('Item_Nbr')['MR_Average_POS'].transform('first')
        df['Priority_Flag'] = df['Average_POS'] >= df['MR_Average_POS']

        # Inventory Allocation
        for item in df['Item_Nbr'].unique():
            item_mask = df['Item_Nbr'] == item
            atp = df.loc[item_mask, 'Available_To_Promise'].iloc[0]
            for priority in [True, False]:
                subset = df[item_mask & (df['Priority_Flag'] == priority)].copy()
                subset = subset.sort_values(by='Average_POS', ascending=False)
                for idx in subset.index:
                    req = df.at[idx, 'Final_Required_Qty']
                    if atp >= req:
                        df.at[idx, 'SSO_Qty'] = req
                        atp -= req
                    else:
                        partial = round_down_to_multiple(atp, df.at[idx, 'WHPK_Qty'])
                        if partial >= df.at[idx, 'WHPK_Qty']:
                            df.at[idx, 'SSO_Qty'] = partial
                            atp -= partial
                        else:
                            break

        # Validate total allocation by (Item_Nbr, Assembly_Warehouse)
        for (item, awh), group in df.groupby(['Item_Nbr', 'Assembly_Warehouse']):
            group_idx = group.index
            total_alloc = df.loc[group_idx, 'SSO_Qty'].sum()
            vnpk = df.loc[group_idx, 'VNPK_Qty'].iloc[0]
            whpk = df.loc[group_idx, 'WHPK_Qty'].iloc[0]
            up_multiple = round_up_to_multiple(total_alloc, vnpk)
            down_multiple = round_down_to_multiple(total_alloc, vnpk)

            if up_multiple == 0:
                continue
            ratio = total_alloc / up_multiple

            if ratio >= 0.6:
                to_add = up_multiple - total_alloc
                sorted_rows = df.loc[group_idx].sort_values(by='Average_POS', ascending=False)
                while to_add > 0:
                    for idx in sorted_rows.index:
                        df.at[idx, 'SSO_Qty'] += whpk
                        to_add -= whpk
                        if to_add <= 0:
                            break
            else:
                to_trim = total_alloc - down_multiple
                sorted_rows = df.loc[group_idx].sort_values(by='Average_POS', ascending=True)
                while to_trim > 0:
                    for idx in sorted_rows.index:
                        if df.at[idx, 'SSO_Qty'] >= whpk:
                            df.at[idx, 'SSO_Qty'] -= whpk
                            to_trim -= whpk
                            if to_trim <= 0:
                                break

        # Final pass to eliminate small total allocations
        for (item, awh), group in df.groupby(['Item_Nbr', 'Assembly_Warehouse']):
            group_idx = group.index
            total_alloc = df.loc[group_idx, 'SSO_Qty'].sum()
            vnpk = df.loc[group_idx, 'VNPK_Qty'].iloc[0]
            up_multiple = round_up_to_multiple(total_alloc, vnpk)
            if up_multiple == 0:
                continue
            ratio = total_alloc / up_multiple
            if ratio < 0.6:
                df.loc[group_idx, 'SSO_Qty'] = 0

        # Display results
        st.success("✅ SSO allocation complete!")

        col1, col2 = st.columns(2)
        col1.metric("Total Records", len(df))
        col2.metric("Total SSO Units", int(df['SSO_Qty'].sum()))

        st.markdown("### 🔍 Preview of Final Output")
        st.dataframe(df.head(20), use_container_width=True)

        # Download
        csv_output = df.to_csv(index=False).encode('utf-8')
        st.download_button("📥 Download CSV", data=csv_output, file_name="Final_SSO_Output.csv", mime="text/csv")

    except Exception as e:
        st.error(f"❌ Error: {e}")

elif process:
    st.warning("⚠️ Please upload both files to proceed.")



