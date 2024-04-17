import streamlit as st
import pandas as pd
from pandas import json_normalize
import requests
import datetime
import altair as alt
import pydeck as pdk
import numpy as np

st.set_page_config(layout="wide", page_title="Police Data API Explorer")

st.sidebar.title("Police Data API Explorer")

st.sidebar.info("Streamlit app that allows the exploration the UK Policing Stop and Search data")

st.sidebar.markdown("The app works by running API requests to [Police Data API](https://data.police.uk/docs/).")
st.sidebar.markdown("With filters for police force and month/year")
st.sidebar.markdown("Information from the query is plot on to a map and is also presented in a table and a graph")
st.sidebar.markdown("The graph tracks the object of search against the logged age range")
st.sidebar.markdown("Information returned from the query can be downloaded as a CSV for later reference")

def police_api():
    
    def police_query():
   
        st.info("Change the selection below to set different parameters for the API query.")

# By building a df object from the returned json values we can use these in a selectbox, capturing the st.session_state key allows us to pass the value back to the API query string
# On value change updates the state and recalls the function
        st.selectbox(
        'Select the Police Force',
        (force_json_df["id"].unique()), key="force", on_change=police_query)
        st.session_state.force

# We can do something similar with the date, but we need to transform the datetime returned value to YYYY-MM format, later rather than calling the key we're going to call the variable that the key updates, because then we can get it in the correct format
# on value change updates the state recalls the function
        date = st.date_input("Select a date", datetime.date(2023, 1, 1),  key="qdate", on_change=police_query)
        date = date.strftime("%Y-%m")
        st.write(date)

# This is better, passing the force and date time values into the API query string for stop and search
        url = (f'https://data.police.uk/api/stops-force?force={st.session_state.force}&date={date}')
        
# error check write the API query string to the page
        st.markdown(f'<h1 style="color:#6ac94f;font-size:12px;">The Police API Query String URL = {url}</h1>', unsafe_allow_html=True)
        
# execute the API call and build the Stop and search df added simple error handling to catch the error if the API call fails   
        try: 
            res = requests.get(url)
            j = res.json()
            police = json_normalize(j)

# rebuild a smaller dataframe with just the plotable columns to pass to st.pydeck_chart
# some error checking to only pull in numeric values and to drop na values in lat column, 
            police["lat"] = police["location.latitude"]
            police['lat'] = pd.to_numeric(police['lat'])
            police["lon"] = police["location.longitude"]
            police['lon'] = pd.to_numeric(police['lon'])
            df = pd.DataFrame(police, columns=['lat', 'lon'])
            df.dropna(subset=['lat'], inplace=True)

# build the st.pydeck_chart configuration and display the map
            st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Map for {st.session_state.force, date}</h1>', unsafe_allow_html=True)
            st.pydeck_chart(pdk.Deck(
            map_style='mapbox://styles/mapbox/light-v9',
            initial_view_state=pdk.ViewState(
                latitude=51.50,
                longitude=-0.1276,
                zoom=7,
                pitch=40,
            ),
            layers=[
                pdk.Layer(
                    'HexagonLayer',
                    data=df,
                    get_position='[lon, lat]',
                    radius=200,
                    elevation_scale=4,
                    elevation_range=[0, 1000],
                    pickable=True,
                    extruded=True,
                ),
                pdk.Layer(
                    'ScatterplotLayer',
                    data=df,
                    get_position='[lon, lat]',
                    get_color='[200, 30, 0, 160]',
                    get_radius=200,
                ),
            ],
            ))

# show the full df from the API call
            st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Table for {st.session_state.force, date}</h1>', unsafe_allow_html=True)
            st.dataframe(police)

# download function for df
            def convert_df(police):
                return police.to_csv().encode('utf-8')

            csv = convert_df(police)

            st.download_button(
                label="Download Output as CSV",
                data=csv,
                file_name=(f'StopandSearch_{st.session_state.force}_{date}.csv'),
                mime='text/csv',
                )

# build and show and altair chart
            st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Chart, age_range and object_of_search {st.session_state.force, date}</h1>', unsafe_allow_html=True)
            chart = alt.Chart(police).mark_area().encode(
            alt.X('age_range:N'),
            alt.Y('count(kind):Q'),
            color='object_of_search:N'
            ).properties(
                width=500,
                height=500
            )
            st.altair_chart(chart, use_container_width=True)
# Error handling for the API call, error appears if nothing returned by JSON stream
        except:
            st.error("Error, please check the API query string - no data returned for the selected Police Force and date")
            st.info("Information is coming directly from the Police Data API, and will be as up to date as this source")

    st.info("Change the selection below to set different parameters for the API query.")
# To pass a force into the stop and search API we need to capture the force IDs from the police API.  
    force = requests.get("https://data.police.uk/api/forces")
    force_json = force.json()
    force_json_df = json_normalize(force_json)
# By building a df object from the returned json values we can use these in a selectbox, capturing the st.session_state key allows us to pass the value back to the API query string
# On value change updates the state and calls the function above
    st.selectbox(
        'Select the Police Force',
        (force_json_df["id"].unique()), key="force", on_change=police_query)
    st.session_state.force

# We can do something similar with the date, but we need to transform the datetime returned value to YYYY-MM format, later rather than calling the key we're going to call the variable that the key updates, because then we can get it in the correct format
# on value change updates the state calls the function above
    date = st.date_input("Select a date", datetime.date(2020, 1, 1),  key="qdate", on_change=police_query)
    date = date.strftime("%Y-%m") 
    st.write(date)

# first API call, implementation without any variables, for the first select and date data inputs
    url = "https://data.police.uk/api/stops-force?force=avon-and-somerset&date=2023-01"

# error check write the API query string to the page
    st.markdown(f'<h1 style="color:#6ac94f;font-size:12px;">The Police API Query String URL = {url}</h1>', unsafe_allow_html=True)

# execute the API call and build the Stop and search df added simple error handling to catch the error if the API call fails 
    try:
        res = requests.get(url)
        j = res.json()
        police = json_normalize(j)

# rebuild a smaller dataframe with just the plotable columns to pass to st.pydeck_chart
# some error checking to only pull in numeric values and to drop na values in lat column, 
        police["lat"] = police["location.latitude"]
        police['lat'] = pd.to_numeric(police['lat'])
        police["lon"] = police["location.longitude"]
        police['lon'] = pd.to_numeric(police['lon'])
        df = pd.DataFrame(police, columns=['lat', 'lon'])
        df.dropna(subset=['lat'], inplace=True)

        st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Map for {st.session_state.force, date}</h1>', unsafe_allow_html=True)
# build the st.pydeck_chart configuration and display the map
        st.pydeck_chart(pdk.Deck(
        map_style='mapbox://styles/mapbox/light-v9',
        initial_view_state=pdk.ViewState(
            latitude=51.50,
            longitude=-0.1276,
            zoom=7,
            pitch=40,
        ),
        layers=[
            pdk.Layer(
                'HexagonLayer',
                data=df,
                get_position='[lon, lat]',
                radius=200,
                elevation_scale=4,
                elevation_range=[0, 1000],
                pickable=True,
                extruded=True,
                ),
            pdk.Layer(
                'ScatterplotLayer',
                data=df,
                get_position='[lon, lat]',
                get_color='[200, 30, 0, 160]',
                get_radius=200,
            ),
        ],
        ))
        

# show the df from the API call
        st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Table for {st.session_state.force, date}</h1>', unsafe_allow_html=True)
        st.dataframe(police)

# download function for the df
        def convert_df(police):
            return police.to_csv().encode('utf-8')
    
        csv = convert_df(police)

        st.download_button(
            label="Download Output as CSV",
            data=csv,
            file_name=(f'StopandSearch_{st.session_state.force}_{date}.csv'),
            mime='text/csv',
            )

# build and show and altair chart
        st.markdown(f'<h1 style="font-size:18px;">Stop and Search Data Chart, age_range and object_of_search {st.session_state.force, date}</h1>', unsafe_allow_html=True)
        chart = alt.Chart(police).mark_area().encode(
        alt.X('age_range:N'),
        alt.Y('count(kind):Q'),
        color='object_of_search:N'
        ).properties(
            width=500,
            height=500
        )
        st.altair_chart(chart, use_container_width=True)
# Error handling for the API call, error appears if nothing returned by JSON stream
    except:
        st.error("Error, please check the API query string - no data returned for the selected Police Force and date")
        st.info("Information is coming directly from the Police Data API, and will be as up to date as this source")
        
if st.button("Click to explore the Police Stop and Search Data API"):
   police_api()
